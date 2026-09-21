"""Keep cobalt's cookie file in step with the jar the bot already has.

Cobalt reads its credentials from its own file (`COOKIE_PATH`) whose shape is not
a Netscape jar: a mapping of service → list of ``Cookie:`` header strings, e.g.
``{"youtube": ["LOGIN_INFO=…; SAPISID=…"]}``. It is the only way to give the
fallback a YouTube session — and cobalt's YouTube path needs *a* cookie to do
more than a plain innertube call (``retrieve_player = Boolean(sessionTokens ||
cookie)``: with one it retrieves the player and its bot-check handling, without
one a flagged IP answers ``error.api.youtube.login``).

The bot's login lives in ``cookies.txt``, so the jar is converted here, on the
same events that already mean "the login changed": startup, and every fresh
export the watcher sees. One export, both engines.

Two properties this module is careful about:

* **Nothing is destroyed.** A file this module did not write is left alone (a
  flat array — the shape cobalt's own docs example suggests — is reported, never
  overwritten), and services that are not ours (``instagram``, ``twitter``, …)
  survive every rewrite. So a deployment that keeps its own ``cookies.json`` for
  other services keeps it.
* **A restart is a fact, not a guess.** Cobalt loads the file once, at startup,
  and writes its own cookie refreshes *back* into it (so the file's mtime stops
  meaning "when we last changed it"). The sidecar written next to it records that
  timestamp — which is what lets the doctor say "cobalt is running an older
  version" instead of guessing from an mtime.
* **An unwritable directory degrades, it does not crash.** The file lives in a
  bind-mounted host directory (``COBALT_COOKIES_DIR``), whose ownership comes from
  the host — so a directory root created is not writable by the bot's uid 10001.
  That used to be a ``PermissionError`` during startup; it is now a warned-about
  state with the exact fix, reported by ``/doctor``
  (:func:`ensure_cookie_dir`), while the fallback simply runs without a session.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.config import Settings
from services.extractor import missing_youtube_login_cookies, read_netscape_cookie_rows

logger = logging.getLogger(__name__)

#: The service key cobalt knows (``VALID_SERVICES`` in its cookie manager). The
#: others (``instagram``, ``twitter``, …) are for logins the bot has no jar for.
SERVICE = "youtube"

#: The file cobalt reads and the sidecar this module writes beside it. Both names
#: are fixed: the compose service points ``COOKIE_PATH`` at the same file.
COBALT_FILE_NAME = "cookies.json"
SIDECAR_FILE_NAME = "generated.json"

#: Where the YouTube session lives. A browser puts the login cookies on
#: ``.youtube.com``; a *Google* export keeps the SID family on ``.google.com``
#: instead, so those names are taken from there too — but only those, because a
#: full Google jar is hundreds of cookies and a request header that large gets
#: refused outright.
SESSION_DOMAINS: tuple[str, ...] = ("youtube.com", "googlevideo.com")
FALLBACK_DOMAIN = "google.com"
#: The cookie families that *are* the session (matching the jar-side check).
SESSION_NAMES: frozenset[str] = frozenset(
    {
        "LOGIN_INFO",
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
    }
)


@dataclass(frozen=True)
class CobaltCookieState:
    """What is on disk for cobalt, and whether we are the ones who put it there.

    ``written`` is true only when the *content* changed in this call — a restart
    rewrites the same bytes and must not look like a new export.
    """

    #: The file cobalt reads; ``None`` when COBALT_COOKIES_DIR is off.
    path: Path | None = None
    written: bool = False
    cookie_count: int = 0
    missing_login: tuple[str, ...] = ()
    #: Keys in the existing file that are not ours (kept, and worth naming).
    other_services: tuple[str, ...] = ()
    #: Why nothing was written — empty when it was, or when there was nothing to do.
    reason: str = ""
    #: When this module last changed the content (from the sidecar), if ever.
    generated_at: float | None = None
    source: str = ""

    @property
    def off(self) -> bool:
        """``COBALT_COOKIES_DIR`` is empty: no file is generated."""
        return self.path is None

    @property
    def usable(self) -> bool:
        """A file with the cookies cobalt needs to retrieve the player."""
        return self.path is not None and self.path.is_file() and self.cookie_count > 0

    def describe(self) -> str:
        """One line for the log and the reports — never invented, never empty.

        Four genuinely different states, said as such: off, on disk, *not written
        yet but the jar has cookies for it*, and nothing to write. Collapsing the
        third into the fourth would blame the jar for a file nobody asked for yet
        (the ``--check`` run before the first generation).
        """
        if self.off:
            return "تولید نمی‌شود (COBALT_COOKIES_DIR خالی است)"
        if self.path is not None and self.path.is_file():
            head = f"{self.path} — {self.cookie_count} کوکی یوتیوب"
            if self.generated_at is not None:
                head += f"، تولید {_ago(time.time() - self.generated_at)}"
            if self.missing_login:
                head += f"، بدون {', '.join(self.missing_login)}"
            return head
        if self.cookie_count:
            head = f"{self.path} — هنوز نوشته نشده ({self.cookie_count} کوکی در جار آماده است)"
            # The reason is *appended* rather than swapped in: the count is real
            # news (the jar is ready), and so is whatever stopped the write.
            return f"{head}؛ {self.reason}" if self.reason else head
        return f"{self.path} — ساخته نشده ({self.reason or 'کوکی یوتیوبی در جار نیست'})"


def cobalt_cookie_paths(settings: Settings) -> tuple[Path | None, Path]:
    """The file cobalt reads and the sidecar beside it (``dir`` may be ``None``)."""
    directory = settings.cobalt_cookies_dir
    if directory is None:
        return None, Path()
    return directory / COBALT_FILE_NAME, directory / SIDECAR_FILE_NAME


def ensure_cookie_dir(directory: Path | None, *, create: bool = True) -> str:
    """Make sure the cookie directory is writable *by this process*.

    Returns ``""`` when it is, and a sentence naming the fix when it is not. The
    failure this exists for is the Docker bind mount: ``docker-compose.yml`` mounts
    the host's ``./cobalt`` over the image's ``/app/cobalt``, and a bind mount keeps
    the *host's* ownership — so a directory created by root (a fresh ``git clone``,
    or an installer that ran as root) is not writable by the bot's uid 10001, and
    the image's own mode is masked by it. Creating the directory is the common case
    (a deployment that never made one) and chmod is the best effort after that;
    when neither works the caller degrades instead of raising, because a fallback
    engine without cookies is worth more than a bot that will not boot.

    ``create=False`` is for read-only callers (``/doctor``, ``/blocks``): they must
    be able to *describe* the problem without creating anything.
    """
    if directory is None:
        return ""
    if create:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"پوشهٔ {directory} ساخته نشد ({exc.strerror or type(exc).__name__})"
    if os.access(directory, os.W_OK):
        return ""
    try:
        os.chmod(directory, 0o777)
    except OSError:
        logger.debug("could not open up %s", directory, exc_info=True)
    if os.access(directory, os.W_OK):
        logger.info("made %s writable for the cobalt cookie file", directory)
        return ""
    return (
        f"پوشهٔ {directory} برای این کاربر قابل نوشتن نیست — روی هاست درستش کنید "
        f"(chmod 777 آن پوشه یا chown 10001) و بات را ری‌استارت کنید؛ تا آن زمان "
        f"کوبالت بدون کوکی اجرا می‌شود"
    )


def cookie_header(rows: list[list[str]]) -> tuple[str, int, int]:
    """Build the ``Cookie:`` header value from Netscape rows.

    Returns ``(header, used, skipped)``. Order is the browser's own within each
    domain, with the session names first — cobalt only ever sends this header to
    YouTube, so the cookies that *are* the login should not be the ones a
    truncating peer drops.

    A value that cannot survive the trip is skipped rather than mangled: cobalt
    splits the string back with ``str.split('; ')``, so a value containing that
    sequence (or a newline) would corrupt the cookies after it.
    """
    chosen: dict[str, str] = {}
    skipped = 0
    for fields in rows:
        domain, name, value = fields[0], fields[5], fields[6]
        host = domain.lstrip("@.").lower()  # '@' shows up in some exporters
        in_session_domain = any(host == d or host.endswith(f".{d}") for d in SESSION_DOMAINS)
        in_fallback_domain = host == FALLBACK_DOMAIN or host.endswith(f".{FALLBACK_DOMAIN}")
        if in_fallback_domain:
            in_session_domain = name in SESSION_NAMES
        if not in_session_domain:
            continue
        if not name or not value or "; " in value or "\n" in value:
            skipped += 1
            continue
        # First writer wins: `youtube.com` is more specific than `.youtube.com`,
        # and both are better than whatever a google.com row repeats.
        chosen.setdefault(name, value)
    ordered = sorted(chosen.items(), key=lambda item: item[0] not in SESSION_NAMES)
    return "; ".join(f"{name}={value}" for name, value in ordered), len(chosen), skipped


def _read_document(path: Path) -> tuple[dict[str, Any] | None, str]:
    """The existing cobalt file as a mapping, or ``(None, why not)``."""
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"خوانده نشد ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "شکلش فهرست است نه mapping — کوبالت هم همین را نمی‌پذیرد"
    return data, ""


def _write_atomically(path: Path, text: str) -> None:
    """Write through a temporary file: cobalt must never read half a document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def sync_from_jar(settings: Settings, *, jar_path: Path | None = None) -> CobaltCookieState:
    """Write cobalt's cookie file from the jar, if that is a thing to do.

    Called at startup and on every fresh export; returns what happened, including
    the reasons *not* to write (feature off, no jar, a file that is not ours).
    """
    path, sidecar = cobalt_cookie_paths(settings)
    if path is None:
        return CobaltCookieState(reason="COBALT_COOKIES_DIR خالی است")
    jar = jar_path if jar_path is not None else settings.cookie_file
    if problem := ensure_cookie_dir(path.parent):
        # Reported, not raised: the bot keeps serving links, the fallback simply
        # runs without a session, and /doctor says which host permission to fix.
        logger.error("cobalt cookies: %s", problem)
        return replace(_read_state(path, sidecar, jar), reason=problem)
    rows = read_netscape_cookie_rows(jar)
    if not rows:
        return replace(
            _read_state(path, sidecar, jar),
            reason="جار کوکی خوانده نشد",
        )

    header, used, skipped = cookie_header(rows)
    existing, broken = _read_document(path)
    if existing is None:
        # Someone else's file, in a shape cobalt would reject anyway: report it
        # rather than replace it — a hand-made cookie file is not ours to lose.
        return replace(_read_state(path, sidecar, jar), reason=broken)
    others = tuple(sorted(key for key in existing if key != SERVICE))

    if not used:
        return replace(
            _read_state(path, sidecar, jar),
            reason="هیچ کوکی یوتیوبی در جار نیست",
            other_services=others,
        )

    document = {**existing, SERVICE: [header]}
    text = json.dumps(document, indent=4, ensure_ascii=False) + "\n"
    changed = True
    try:
        changed = not path.is_file() or path.read_text(encoding="utf-8") != text
    except OSError:
        changed = True

    missing = missing_youtube_login_cookies(jar)
    state = CobaltCookieState(
        path=path,
        # Not yet written: the flag means "this call changed the file", and it
        # only earns that after the write below actually succeeded.
        written=False,
        cookie_count=used,
        missing_login=missing,
        other_services=others,
        source=str(jar) if jar is not None else "",
    )
    if not changed:
        return replace(state, generated_at=_read_sidecar(sidecar))
    # One stamp for the sidecar and for the returned state: two calls to time.time()
    # differ by microseconds, and the doctor compares this number against cobalt's
    # start time — "the stamp survived the restart" has to be exactly true.
    stamp = time.time()
    try:
        _write_atomically(path, text)
        _write_atomically(
            sidecar,
            json.dumps(
                {
                    "at": stamp,
                    "cookies": used,
                    "missing_login": list(missing),
                    "source": state.source,
                    "services": [SERVICE],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
    except OSError as exc:
        # The same trap as an unwritable directory, one step later — a write that
        # fails must not take the bot down with it.
        problem = (
            f"نوشتن در {path} ممکن نشد ({exc.strerror or type(exc).__name__}) — "
            f"دسترسی پوشه را روی هاست درست کنید"
        )
        logger.error("cobalt cookies: %s", problem)
        return replace(state, reason=problem)
    logger.info(
        "cobalt cookies: %s youtube cookie(s) written to %s%s%s",
        used,
        path,
        f" (skipped {skipped})" if skipped else "",
        f", keeping {', '.join(others)}" if others else "",
    )
    return replace(state, written=True, generated_at=stamp)


def _read_sidecar(sidecar: Path) -> float | None:
    """When this module last changed the content (``None`` when unknown)."""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return float(data["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _read_state(path: Path, sidecar: Path, jar: Path | None) -> CobaltCookieState:
    """What is on disk right now — read-only, for reports that must not write."""
    document, _ = _read_document(path) if path.is_file() else ({}, "")
    header, used, _ = cookie_header(read_netscape_cookie_rows(jar))
    # The *file* is what cobalt reads, so count it when it is there: after a
    # generation the two agree, and when they do not (a file somebody else wrote,
    # or a jar that moved on) the report must describe the artifact, not the source.
    entries = document.get(SERVICE) if isinstance(document, dict) else None
    if isinstance(entries, list) and entries and isinstance(entries[0], str):
        used = len([part for part in entries[0].split("; ") if part]) or used
    sidecar_data: dict[str, Any] = {}
    try:
        sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sidecar_data = {}
    services = document or {}
    missing = sidecar_data.get("missing_login")
    return CobaltCookieState(
        path=path,
        written=False,
        # The jar is the source of truth for the count when it is readable; the
        # sidecar covers the case where the jar is gone but the file is not.
        cookie_count=used or int(sidecar_data.get("cookies") or 0),
        missing_login=tuple(missing) if isinstance(missing, list) else (),
        other_services=tuple(sorted(key for key in services if key != SERVICE)),
        generated_at=_read_sidecar(sidecar),
        source=str(jar) if jar is not None else "",
    )


def read_state(settings: Settings, *, jar_path: Path | None = None) -> CobaltCookieState:
    """Describe the generated file without touching it (``/doctor``, scripts).

    An unwritable directory is *reported here* rather than discovered at the next
    export: it is the one failure an operator can only fix on the host, and finding
    it while reading the report is the cheapest possible moment to find it.
    """
    path, sidecar = cobalt_cookie_paths(settings)
    if path is None:
        return CobaltCookieState(reason="COBALT_COOKIES_DIR خالی است")
    state = _read_state(
        path, sidecar, jar_path if jar_path is not None else settings.cookie_file
    )
    problem = ensure_cookie_dir(path.parent, create=False)
    return replace(state, reason=problem) if problem else state


def restart_needed(generated_at: float | None, started_at: float | None) -> bool | None:
    """Whether cobalt is running on an older cookie file.

    ``None`` means "cannot tell" (no file of ours, or the instance did not say
    when it started) — deliberately distinct from ``False``: a report that
    guesses here would either cry wolf after every restart or stay quiet when the
    fix is one ``docker compose restart cobalt`` away.
    """
    if generated_at is None or started_at is None:
        return None
    # A second of slack: cobalt reads the file during startup, so a generation a
    # fraction of a second before its start time is the version it loaded.
    return generated_at > started_at + 1.0


def _ago(seconds: float) -> str:
    """Small local formatter: the doctor has its own, this one must not import it."""
    if seconds < 90:
        return "همین حالا"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} دقیقه پیش"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} ساعت پیش"
    return f"{hours / 24:.0f} روز پیش"


#: Kept for callers that want the raw mode of the file (tests, scripts).
__all__ = [
    "COBALT_FILE_NAME",
    "SIDECAR_FILE_NAME",
    "SERVICE",
    "CobaltCookieState",
    "cobalt_cookie_paths",
    "cookie_header",
    "read_state",
    "restart_needed",
    "sync_from_jar",
]
