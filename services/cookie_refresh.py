"""Re-export the cookie jar from a browser profile, then prove it with a probe.

A jar that cannot sign in is the one failure the operator can actually fix — and
"the operator can fix it" usually means the mechanical part: read the cookies the
browser already holds, write them where the bot reads them, and check the result.
This does exactly that when a profile is reachable (a host run, or a mounted
profile — inside a plain container there is no browser to read), for the trigger
that matters: a download just failed because YouTube treated us as anonymous.

Four rules keep it from making things worse:

* **Look before spending an attempt.** If the browser's profile is not where this
  platform keeps it (a plain container), the attempt is skipped and the admins are
  told to run the export where the browser is — instead of a wasted export and a
  confusing yt-dlp error.
* **Never replace a working login.** The candidate must be a usable jar, and if the
  current jar signs in while the candidate does not, the candidate is thrown away.
* **Write atomically.** The jar is built beside the target and moved into place, so
  a reader (yt-dlp in a worker thread) never sees a half-written cookie file.
* **Say what happened.** The probe verdict rides along to the admins, including
  "the profile is not reachable from here" — which is the answer inside a container.

The same import step is what the admin's ``/refresh``, the alert's button and the
login wizard use: one implementation of the rules, three ways to ask for it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import asyncpg
from aiogram import Bot
from aiogram.enums import ParseMode

from core.config import Settings
from core.utils import escape_html
from services import telemetry
from services.cookie_watch import verify_export
from services.extractor import (
    BrowserSpecError,
    ExtractorService,
    app_bound_encryption_active,
    browser_profile_paths,
    browser_profile_reachable,
    cookie_jar_is_usable,
    missing_youtube_login_cookies,
    parse_browser_spec,
)

logger = logging.getLogger(__name__)

#: How often the automatic refresh may run. A login-shaped block repeats with
#: every link while the jar is broken, and re-exporting the same profile again a
#: minute later cannot possibly help.
DEFAULT_COOLDOWN_S = 1800.0


class ExportError(RuntimeError):
    """A readable failure instead of yt-dlp's generic CookieLoadError."""


@dataclass(frozen=True)
class RefreshOutcome:
    """What the automatic refresh did, and what to tell the admins about it."""

    kind: Literal["replaced", "kept", "unreachable", "failed"]
    message: str
    cookies: int = 0
    verdict: str = ""


@dataclass(frozen=True)
class ImportResult:
    """What reading a browser profile into the jar did (the shared first half).

    Separate from :class:`RefreshOutcome` because three callers need exactly this
    much and nothing more: the automatic refresh (which then probes), the admin's
    ``/refresh`` and the login wizard (which then probes and asks its own
    questions).
    """

    kind: Literal["replaced", "kept", "unreachable", "failed"]
    message: str = ""
    cookies: int = 0


#: In-process state, deliberately not in the database: this guards one process's
#: enthusiasm, and a restart is allowed to try again.
_last_attempt_at: float | None = None
_in_flight = False


def reset_state() -> None:
    """Forget the cooldown and the in-flight flag (used by tests and operators)."""
    global _last_attempt_at, _in_flight
    _last_attempt_at = None
    _in_flight = False


def cooldown_remaining(now: float | None = None, *, cooldown_s: float = DEFAULT_COOLDOWN_S) -> float:
    """Seconds until the next automatic attempt is allowed (0 when it is due)."""
    if _last_attempt_at is None:
        return 0.0
    moment = time.monotonic() if now is None else now
    return max(0.0, cooldown_s - (moment - _last_attempt_at))


def export_jar_from_browser(spec: str, output: Path, *, verbose: bool = False) -> int:
    """Write a Netscape cookie jar for ``spec``; returns the number of cookies.

    Blocking (yt-dlp reads the profile synchronously) — callers run it in a
    thread. Raises :class:`ExportError` with something readable on failure.
    """
    import yt_dlp
    from yt_dlp.cookies import load_cookies

    browser, profile, keyring, container = parse_browser_spec(spec)
    logger.info(
        "reading cookies from %s (profile=%s, keyring=%s)",
        browser,
        profile or "auto",
        keyring or "auto",
    )
    try:
        with yt_dlp.YoutubeDL(
            {"quiet": not verbose, "no_warnings": not verbose, "verbose": verbose}
        ) as ydl:
            jar = load_cookies(None, (browser, profile, keyring, container), ydl)
    except Exception as exc:  # noqa: BLE001 — yt-dlp raises a generic CookieLoadError
        raise ExportError(f"could not read cookies from {browser!r}: {exc}") from exc
    if not len(jar):
        raise ExportError("the profile was readable but contained no cookies")

    clear_docker_placeholder(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    jar.save(str(output), ignore_discard=True, ignore_expires=True)
    return len(jar)


def clear_docker_placeholder(output: Path) -> None:
    """Remove the empty directory Docker leaves when a mounted file is missing.

    Bind-mounting a *file* whose host path does not exist makes Docker create a
    directory there instead, so a first-time export can find ``cookies.txt/``
    where ``cookies.txt`` should be and ``jar.save()`` fails with a bare
    ``IsADirectoryError``. Removing an empty placeholder is safe; anything else in
    there means the operator made that directory on purpose, so leave it.
    """
    if not output.is_dir():
        return
    try:
        entries = list(output.iterdir())
    except OSError as exc:
        raise ExportError(f"{output} is a directory and cannot be read: {exc}") from exc
    if entries:
        raise ExportError(
            f"{output} is a directory, not a cookie jar, and it is not empty. "
            "Move its contents away (or point COOKIE_FILE somewhere else) first."
        )
    output.rmdir()
    logger.info(
        "%s was an empty directory (Docker creates one for a missing bind-mount "
        "source) — removed it and writing the jar there.",
        output,
    )


def candidate_rejection(candidate: Path, current: Path | None) -> str | None:
    """Why the candidate must be rejected, or ``None`` when it may replace.

    The one irreversible mistake here is losing a login that worked, so a
    candidate without one never overwrites a jar that has one.
    """
    if not cookie_jar_is_usable(candidate):
        return "اکسپورت نتیجهٔ قابل‌استفاده‌ای نداشت (فایل خالی/ناقص)"
    candidate_missing = missing_youtube_login_cookies(candidate)
    if candidate_missing and current is not None and cookie_jar_is_usable(current):
        if not missing_youtube_login_cookies(current):
            return (
                "اکسپورت تازه لاگین یوتیوب نداشت، پس جارِ سالمِ فعلی دست‌نخورده ماند "
                f"(کم: {', '.join(candidate_missing)}) — در آن مرورگر وارد یوتیوب شوید"
            )
    return None


def app_bound_message(spec: str) -> str:
    """Why this profile cannot be read from outside the browser, and what can."""
    try:
        browser = parse_browser_spec(spec)[0]
    except BrowserSpecError:
        browser = spec
    return (
        f"مرورگرِ {browser} کوکی‌هایش را با App-Bound Encryption ویندوز قفل کرده و "
        "هیچ برنامه‌ای بیرون از خود مرورگر (از جمله yt-dlp) نمی‌تواند رمزگشایی‌شان "
        "کند — این خطا ربطی به لاگین‌بودن یا نبودن ندارد.\n"
        "راه‌هایی که واقعاً کار می‌کنند:\n"
        "• اکسپورت با افزونه‌ای که include HttpOnly را دارد (کوکی‌های لاگین فقط "
        "HttpOnly هستند).\n"
        "• یا Firefox برای این کار (این قفل را ندارد) و بعد "
        "COOKIE_AUTO_EXPORT/COOKIES_FROM_BROWSER روی firefox.\n"
        "• یا همان روش دستی: اکسپورت بگیرید و cookies.txt را جایگزین کنید."
    )


def no_profile_message(spec: str) -> str:
    """Why no attempt was made here, and where we looked (plain text)."""
    try:
        browser = parse_browser_spec(spec)[0]
        paths = browser_profile_paths(spec)
    except BrowserSpecError as exc:
        return f"پروفایل نامعتبر ({exc})"
    looked = "\n".join(f"• {path}" for path in paths) or "• (مسیری برای این مرورگر نمی‌شناسیم)"
    return (
        f"از این اجرا مرورگرِ {browser} دیده نشد، پس چیزی برای خواندن نبود و اصلاً "
        f"تلاش نکردم.\nمسیرهایی که گشته شد:\n{looked}"
    )


async def import_from_profile(
    spec: str, target: Path, *, check_reachable: bool = True
) -> ImportResult:
    """Read a browser profile into ``target``: atomically, and never worse than now.

    Shared by the automatic refresh, the admin's ``/refresh`` and the login wizard,
    so all three replace the jar the same way — usable candidate only, written
    beside the target and moved into place, and never losing a login that works.
    """
    if check_reachable and app_bound_encryption_active(spec):
        # The profile is here and readable as a *file* — it is the cookie database
        # that is locked, with the browser's own key. yt-dlp cannot open it at all,
        # so the attempt would only produce "failed to load cookies".
        return ImportResult("unreachable", app_bound_message(spec))
    if check_reachable and browser_profile_reachable(spec) is False:
        # The cheap answer first: on a host with no browser (a plain container)
        # this saves the attempt, the wait, and the confused yt-dlp error — and
        # the message says where we looked, which is the operator's next move.
        return ImportResult("unreachable", no_profile_message(spec))

    candidate = target.parent / f".{target.name}.auto-{os.getpid()}.tmp"
    try:
        try:
            cookies = await asyncio.to_thread(export_jar_from_browser, spec, candidate)
        except ExportError as exc:
            return ImportResult("unreachable", str(exc))
        except OSError as exc:
            return ImportResult(
                "unreachable",
                f"نوشتن جار ممکن نشد ({exc}) — روی این میزبان پروفایل مرورگر قابل‌دسترس "
                "نیست یا مسیر فقط-خواندنی است",
            )
        except Exception as exc:  # noqa: BLE001 — a refresh must never crash a worker
            logger.exception("automatic cookie refresh failed while exporting")
            return ImportResult("failed", f"{type(exc).__name__}: {exc}")

        if (rejection := candidate_rejection(candidate, target)) is not None:
            return ImportResult("kept", rejection, cookies=cookies)

        try:
            os.replace(candidate, target)
        except OSError as exc:
            return ImportResult(
                "unreachable",
                f"جار تازه نوشته شد ولی جای‌گذاری نشد ({exc}) — روی کانتینر جار "
                "فقط-خواندنی است؛ اکسپورت را روی هاست بزنید",
                cookies=cookies,
            )

        logger.info("cookie jar replaced from profile %s (%s cookies)", spec, cookies)
        return ImportResult("replaced", "", cookies=cookies)
    finally:
        # Never leave a half-built jar behind (a failed move, a rejected candidate).
        # On a read-only mount there is nothing to remove and the attempt itself
        # fails with EROFS — the outcome above already said what happened, so a
        # cleanup that cannot run must not become the error the caller sees.
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not remove the temporary %s", candidate, exc_info=True)


async def auto_refresh_jar(
    settings: Settings,
    extractor: ExtractorService,
    bot: Bot | None,
    admin_ids: Iterable[int] = (),
    *,
    cooldown_s: float = DEFAULT_COOLDOWN_S,
    now: float | None = None,
    force: bool = False,
    spec: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> RefreshOutcome | None:
    """Re-export the jar from a profile, verify it, tell the admins.

    Returns ``None`` when nothing was attempted: no profile, a refresh already
    running, or (without ``force``) the cooldown has not passed.

    ``force`` is what ``/refresh`` and the alert's button use — a human asked, so
    the cooldown does not apply, while the guard rails that actually matter (usable
    candidate, never lose a working login, atomic write) still do. ``spec`` names
    the profile to read, which is how an admin tries one without a restart. ``pool``
    records the fix, which is what makes ``/trend`` able to say whether it helped.
    """
    global _last_attempt_at, _in_flight

    chosen = (spec if spec is not None else settings.cookie_auto_export).strip()
    target = settings.cookie_file
    if not chosen or target is None:
        logger.debug("cookie refresh is off (no profile named)")
        return None
    try:
        parse_browser_spec(chosen)
    except BrowserSpecError as exc:
        # A typo in COOKIE_AUTO_EXPORT (or an admin's /refresh argument) would
        # otherwise surface as a yt-dlp error mid-download, once per failing link.
        outcome = RefreshOutcome("failed", f"پروفایل نامعتبر — {exc}")
        _last_attempt_at = time.monotonic() if now is None else now
        await _tell_admins(bot, admin_ids, outcome)
        return outcome
    if _in_flight:
        logger.info("cookie refresh is already running — not starting another")
        return None
    if not force:
        remaining = cooldown_remaining(now, cooldown_s=cooldown_s)
        if remaining > 0:
            logger.info(
                "cookie refresh skipped: last attempt was %.0fs ago (cooldown %.0fs)",
                cooldown_s - remaining,
                cooldown_s,
            )
            return None

    _in_flight = True
    _last_attempt_at = time.monotonic() if now is None else now
    try:
        try:
            outcome = await _refresh(chosen, target, extractor)
        except Exception as exc:  # noqa: BLE001 — see below
            # This runs *inside* the failure path of a download that already went
            # wrong. Whatever happens here, the user's message and the admins'
            # notice must still go out, so nothing may escape this call.
            logger.exception("cookie refresh raised; the failure path continues")
            outcome = RefreshOutcome("failed", f"{type(exc).__name__}: {exc}")
    finally:
        _in_flight = False

    if outcome.kind == "replaced" and pool is not None:
        # The trend compares failures on either side of this row, so it is written
        # once per actual replacement — not per attempt.
        source = "دستی: " if force else ""
        await telemetry.record_fix(
            pool, kind="cookie_jar", detail=f"{source}{chosen} → {outcome.cookies} کوکی"
        )

    await _tell_admins(bot, admin_ids, outcome)
    return outcome


async def _refresh(spec: str, target: Path, extractor: ExtractorService) -> RefreshOutcome:
    """Import the profile into the jar, then prove the result with a probe."""
    result = await import_from_profile(spec, target)
    if result.kind != "replaced":
        return RefreshOutcome(result.kind, result.message, cookies=result.cookies)
    # Probe *after* the replacement: this is also what hands the new jar to yt-dlp
    # (the writable copy is refreshed), which keeps the watcher from announcing the
    # same export a second time.
    verdict = await verify_export(extractor, extractor.cookie_jar_state())
    return RefreshOutcome("replaced", "", cookies=result.cookies, verdict=verdict)


def render_outcome(outcome: RefreshOutcome) -> str:
    """The outcome as the admins read it."""
    if outcome.kind == "replaced":
        return (
            f"🍪 <b>جار کوکی خودکار تازه شد</b> ({outcome.cookies} کوکی از پروفایل "
            "مرورگر) و همین حالا تست شد:\n\n"
            f"{outcome.verdict}"
        )
    if outcome.kind == "kept":
        return f"🍪 اکسپورت خودکار انجام شد ولی جای‌گذاری نشد: {escape_html(outcome.message)}"
    if outcome.kind == "unreachable":
        return (
            "🍪 <b>اکسپورت خودکار کوکی ممکن نشد</b>\n\n"
            f"{escape_html(outcome.message)}\n\n"
            "👉 روی همان ماشینی که مرورگر لاگین‌شده دارد اجرا کنید: "
            "<code>python scripts/export_cookies.py</code> — جارِ mountشده خودش برداشته می‌شود."
        )
    return f"⚠️ اکسپورت خودکار کوکی انجام نشد: {escape_html(outcome.message)}"


async def _tell_admins(bot: Bot | None, admin_ids: Iterable[int], outcome: RefreshOutcome) -> None:
    """Report the outcome; a refresh nobody hears about is a refresh half done."""
    admins = list(admin_ids)
    if bot is None or not admins:
        logger.warning("cookie refresh outcome not delivered: %s", outcome.kind)
        return
    text = render_outcome(outcome)
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("could not tell admin %s about the cookie refresh", admin_id)
