"""One-shot diagnosis of the YouTube download path.

YouTube blocks look alike from the outside: a cookie jar that is not really
logged in, a PO-token provider that is down, a missing JavaScript runtime and a
flagged IP all end as "Sign in to confirm you're not a bot". This module turns
the whole chain into an ordered list of checks plus a single verdict and the one
next step worth taking, so neither an operator nor an admin has to guess.

The same report backs ``scripts/youtube_doctor.py`` (CLI) and the admin-only
``/doctor`` Telegram command.

One section is different from the rest: the *fallback* engine (see
``services/fallback.py``) is not part of the yt-dlp chain, it is what happens when
that chain is refused. It gets its own line, its own four states, and a live
probe — because "COBALT_API_URL is set" and "a blocked link will actually be
served" are different facts, and only the second one keeps a user out of it.

Two checks speak for the helper servers this stack runs — the PO-token provider
that signs yt-dlp's requests to YouTube, and the session server that gives the
fallback engine a way past the same bot check without any login. Both are *local*
and both are optional: a check that finds one down says what is lost and what the
one command is, rather than dressing a missing helper up as a broken bot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import asyncpg

from core import database
from core.config import (
    COMPOSE_LOOPBACK_PORTS,
    COMPOSE_SERVICE_ENV,
    Settings,
    cobalt_instance_is_local,
    in_container,
    probe_url,
)
from services import cobalt_cookies
from services.cobalt import CobaltError, CobaltNodeState, CobaltService
from services.cobalt_cookies import CobaltCookieState
from services.extractor import (
    BLOCK_EXTRACTION_CODES,
    CookieJarState,
    ExtractionError,
    ExtractorService,
    missing_youtube_login_cookies,
    pot_plugin_installed,
    pot_plugin_version,
)
from services.fallback import FallbackUse, last_use

logger = logging.getLogger(__name__)

Status = Literal["ok", "warn", "fail"]

#: Metadata-only probe: a public, stable video. Extraction is the step YouTube
#: blocks, so this exercises the whole chain without downloading anything.
DEFAULT_PROBE_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"

#: The check that speaks for yt-dlp's PO-token provider (bgutil HTTP server).
POT_CHECK_NAME = "PO token"
#: ...and the one for the fallback engine's YouTube session server.
SESSION_CHECK_NAME = "سرور سشن یوتیوب"

#: Where each helper answers: bgutil reports its version on ``/ping``, and the
#: session server hands out the token on ``/token`` — that second path is not ours
#: to choose, it is the one cobalt asks (its ``youtube-session.js``).
POT_PING_PATH = "/ping"
SESSION_TOKEN_PATH = "/token"

#: Cobalt re-reads the session server on its own — the interval is built into the
#: instance (5 minutes), so nothing needs a restart to pick a fresh token up.
SESSION_RELOAD_NOTE = "کوبالت هر ۵ دقیقه خودش دوباره میخواند"


_ICONS: dict[Status, str] = {"ok": "✅", "warn": "⚠️", "fail": "⛔️"}


@dataclass(frozen=True)
class PotProvider:
    """What the PO-token provider answered (``GET /ping``)."""

    reachable: bool
    version: str = ""
    error: str = ""


@dataclass(frozen=True)
class SessionServer:
    """What the YouTube session server has ready (``GET /token``).

    ``ready`` is the only state in which the fallback has a session: the server
    answers ``503`` while Chromium is still producing its first token, and that is a
    different fact from a port nobody is listening on.
    """

    reachable: bool
    ready: bool = False
    age: float | None = None
    token_length: int = 0
    error: str = ""

    @property
    def short(self) -> bool:
        """Whether the token is shorter than cobalt considers trustworthy."""
        return self.ready and self.token_length < 160


@dataclass(frozen=True)
class Check:
    """One link in the chain, with the evidence that produced its status."""

    name: str
    status: Status
    detail: str = ""
    #: Replaces the status icon when a check has more states than the three the
    #: verdict is built from — the fallback engine is ready / quarantined /
    #: needs-a-key / unreachable / off, and lumping those into one ⛔️ would hide
    #: exactly the distinction an admin is looking for.
    icon: str | None = None
    #: Start a new visual group: the fallback is a different chain, and reading it
    #: as one more step of the yt-dlp one is the confusion this avoids.
    section: bool = False


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[Check, ...]
    verdict: str
    next_step: str

    @property
    def healthy(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def render(self) -> str:
        """Compact text, ready for a terminal or a Telegram message."""
        lines = ["🩺 دکتر یوتیوب", "━━━━━━━━━━━━━━━"]
        for check in self.checks:
            if check.section and lines[-1]:
                lines.append("")
            icon = check.icon or _ICONS[check.status]
            lines.append(f"{icon} {check.name}" + (f": {check.detail}" if check.detail else ""))
        lines += ["", f"حکم: {self.verdict}", f"قدم بعدی: {self.next_step}"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The fallback engine (Cobalt): a section of its own
# ---------------------------------------------------------------------------

FALLBACK_CHECK_NAME = "موتور جایگزین (Cobalt)"
FALLBACK_SECTION_TITLE = "موتور جایگزین"

#: The generated cookie file is its own line: it decides whether the fallback can
#: serve YouTube at all, and it is the one piece that needs a *restart* to take
#: effect (cobalt reads it once, at startup).
COBALT_COOKIE_CHECK_NAME = "کوکی کوبالت"

#: What to do when cobalt is running on an older file — the whole reason this is
#: said out loud instead of leaving it to the user's next failed download.
COBALT_RESTART_FIX = "docker compose restart cobalt"


async def cobalt_cookie_check(
    settings: Settings,
    cobalt: CobaltService | None,
    *,
    jar_path: Path | None = None,
    state: CobaltCookieState | None = None,
    probe: bool = True,
) -> Check:
    """The cookie file the fallback engine reads: what it holds, and whether the
    running instance has it.

    Three facts, each from a different place and none of them guessed: the file
    and its stamp from disk (free, so ``/blocks`` shows it too), the login check
    from the same jar-side rule yt-dlp's own check uses, and the comparison with
    cobalt's start time from the instance itself — the only way to know a restart
    is what stands between the file and the running bot.
    """
    state = state or cobalt_cookies.read_state(settings, jar_path=jar_path)
    if state.off or not state.usable:
        # ``describe()`` is the one place that keeps the states apart: off, nothing
        # to write, and *the jar is fine, nobody has generated it yet*. A doctor
        # that flattened them would blame the jar for an absent file.
        return Check(COBALT_COOKIE_CHECK_NAME, "warn", state.describe())

    started_at = await cobalt.server_start_time() if probe and cobalt is not None else None
    stale = cobalt_cookies.restart_needed(state.generated_at, started_at)
    facts = state.describe()
    if stale is True:
        return Check(
            COBALT_COOKIE_CHECK_NAME,
            "warn",
            f"{facts}\n     ⚠️ کوبالت پیش از این نسخه بالا آمده — همین حالا نسخهٔ قدیمی را در حافظه دارد\n"
            f"     قدم بعدی: {COBALT_RESTART_FIX}",
            icon="🍪",
        )
    if stale is False:
        return Check(
            COBALT_COOKIE_CHECK_NAME,
            "ok",
            f"{facts}؛ کوبالت همین نسخه را خوانده",
            icon="🍪",
        )
    # Cannot tell: either the instance did not say when it started, or this file
    # was not generated by this deployment. Say which, not a reassuring "fine".
    why = (
        "این فایل را این استقرار نساخته"
        if state.generated_at is None
        else "زمان بالا آمدن کوبالت معلوم نشد"
    )
    return Check(
        COBALT_COOKIE_CHECK_NAME,
        "warn",
        f"{facts} — {why}"
        + (f"؛ برای اطمینان: {COBALT_RESTART_FIX}" if state.generated_at is None else ""),
        icon="🍪",
    )
#: The short form, for one-line messages — with ``(داخلی)`` added when the instance
#: is the one this stack runs (see :attr:`FallbackHealth.embedded`).
FALLBACK_SHORT_NAME = "موتور جایگزین"
#: Where the last verdict is kept, so a restart (or an offline run) still answers
#: "was the safety net working?" instead of shrugging.
FALLBACK_STATE_KEY = "cobalt_status"

#: The five answers an operator needs, with the state the verdict logic uses.
#: ``ready`` is the only one that means a blocked link has somewhere to go.
FALLBACK_TITLES: dict[str, str] = {
    "ready": "آماده به کار",
    "quarantined": "قرنطینه",
    "auth": "نیازمند کلید احراز هویت",
    "youtube": "برای یوتیوب سشن/کوکی ندارد",
    "unreachable": "در دسترس نیست",
    "degraded": "پاسخ داد، ولی این لینک را نگرفت",
    "off": "خاموش",
    "unknown": "تست نشد",
}
FALLBACK_ICONS: dict[str, str] = {
    "ready": "🟢",
    "quarantined": "🟡",
    "degraded": "🟡",
    # Red for both credential gaps: neither serves the link in front of you.
    "auth": "🔴",
    "youtube": "🔴",
    "unreachable": "❌",
    "off": "⚫️",
    "unknown": "❔",
}
#: What the one-line fix is, per state (the report should not just say "broken").
FALLBACK_FIXES: dict[str, str] = {
    "auth": "یک نمونهٔ خودتان اجرا کنید (ghcr.io/imputnet/cobalt) یا COBALT_API_KEY بگذارید",
    # Not "mount cookies.json": that file is generated from the bot's own jar
    # (COBALT_COOKIES_DIR) and the line above says whether cobalt has it — so the
    # missing piece is a *signed-in* jar, or the session server for an IP that
    # even a login does not satisfy.
    "youtube": (
        "دو راه دارد و هر دو در /doctor خط وضعیت دارند: یک اکسپورت با لاگین یوتیوب "
        "(scripts/export_cookies.py — خودکار اینجا نوشته می‌شود)، یا سرور سشن "
        "(YOUTUBE_SESSION_SERVER) که به لاگین نیاز ندارد"
    ),
    "unreachable": "آدرس/شبکهٔ نمونه را بررسی کنید؛ از این هاست باید قابل دسترس باشد (COBALT_PROXY)",
    "quarantined": "به‌زودی خودش دوباره امتحان می‌شود؛ علت بالا را رفع کنید",
    "degraded": "نمونه سالم است؛ این لینک را نگرفت (خود لینک/سایت را ببینید)",
    "off": "اختیاری است — رفتار بلاک‌ها همان قبلی می‌ماند",
    "unknown": "با /doctor کامل (بدون probe=False) یا python scripts/youtube_doctor.py تست کنید",
}


@dataclass(frozen=True)
class FallbackHealth:
    """What the fallback engine is doing, in one state plus the evidence.

    The same shape for a live probe, an in-process quarantine and a value read back
    from the database — so "last known" and "just asked" render identically and
    neither can be mistaken for the other (``remembered`` says which it is).
    """

    state: str
    url: str = ""
    dialect: str | None = None
    seconds: float | None = None
    reason: str = ""
    remembered: bool = False
    #: What the net did the last time a *real* link needed it (``services.fallback``).
    #: A probe answers "would it answer?"; this answers "did it, when it counted?"
    use: FallbackUse | None = None
    #: Every node in the pool, when there is more than one. Left out of ``stored()``
    #: on purpose: a remembered verdict is about *one* address, and handing a stale
    #: node list to the next run would report a pool that no longer exists.
    nodes: tuple[CobaltNodeState, ...] = ()

    @property
    def icon(self) -> str:
        return FALLBACK_ICONS.get(self.state, "❔")

    @property
    def title(self) -> str:
        return FALLBACK_TITLES.get(self.state, self.state)

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def fix(self) -> str:
        return FALLBACK_FIXES.get(self.state, "")

    @property
    def embedded(self) -> bool:
        """Whether this is the instance the stack runs itself (same host, port 9000).

        Worth naming in the report rather than leaving to the URL: it decides what a
        broken fallback *means*. The embedded one shares this host's address, so a
        fix for it is a session or a proxy; an instance somewhere else may simply be
        the other address that gets past an IP block.
        """
        return cobalt_instance_is_local(self.url)

    @property
    def label(self) -> str:
        """``موتور جایگزین``, marked as the embedded one when it is."""
        return f"{FALLBACK_SHORT_NAME} (داخلی)" if self.embedded else FALLBACK_SHORT_NAME

    def about(self, url: str) -> bool:
        """Whether this verdict is evidence about *that* instance.

        A remembered verdict outlives the process that recorded it, but an operator
        who repoints ``COBALT_API_URL`` is asking a new question — and "🔴 needs a
        key" describes a machine that is no longer configured. The embedded instance
        answers to several addresses (its compose name in the network, its published
        loopback port on the host), and those are one machine, not a repoint.
        """
        if not self.url or not url:
            return True
        if self.url.rstrip("/") == url.rstrip("/"):
            return True
        return cobalt_instance_is_local(self.url) and cobalt_instance_is_local(url)

    @property
    def stale(self) -> bool:
        """Whether this is *not* a fresh observation of the instance.

        A remembered verdict and a run that never asked are both worth saying out
        loud — but only these two, because a live quarantine *is* fresh evidence.
        """
        return self.state == "unknown" or self.remembered

    def use_line(self, indent: str = "     ") -> str:
        """The real-traffic evidence, when there is any (never invented)."""
        if self.use is None:
            return ""
        line = f"{indent}آخرین لینک بلاک‌شده: {describe_age(self.use.seconds)} — {self.use.label}"
        if self.use.reason:
            line += f"\n{indent}     {self.use.reason}"
        return line

    def detail(self) -> str:
        """URL, state, dialect, latency, reason and the fix — what to diagnose from."""
        tail = [self.title]
        if self.dialect:
            tail.append(self.dialect)
        if self.seconds is not None:
            tail.append(f"{self.seconds:.1f}s")
        if self.remembered:
            tail.append("آخرین نتیجهٔ ثبت‌شده")
        where = f"{self.url} (داخلی)" if self.embedded else self.url
        line = f"{where} — {' • '.join(tail)}" if where else " • ".join(tail)
        if self.reason:
            line += f"\n     علت: {self.reason}"
        if pool := _pool_line(self.nodes):
            line += f"\n{pool}"
        if use := self.use_line():
            line += f"\n{use}"
        if fix := self.fix:
            line += f"\n     قدم بعدی: {fix}"
        return line

    def line(self) -> str:
        """The one-line summary, for messages that are not the doctor report.

        Same facts as :meth:`detail`, told short: ``/blocks`` is read for the
        failures, and the net's health is the paragraph that decides whether they
        were the user's problem or ours.
        """
        tail = [f"{self.icon} {self.title}"]
        if self.dialect:
            tail.append(self.dialect)
        if self.seconds is not None:
            tail.append(f"{self.seconds:.1f}s")
        if self.remembered:
            tail.append("آخرین نتیجهٔ ثبت‌شده")
        # No `(داخلی)` on the URL here: the label above already carries it, and
        # saying it twice reads as two different facts.
        head = f"🔌 {self.label}: {self.url} — {' • '.join(tail)}" if self.url else (
            f"🔌 {self.label}: {' • '.join(tail)}"
        )
        lines = [head]
        duplicate = self.use.reason if self.use is not None else ""
        if self.reason and self.reason != duplicate:
            lines.append(f"     علت: {self.reason}")
        if pool := _pool_line(self.nodes):
            lines.append(pool)
        if use := self.use_line():
            lines.append(use)
        if not self.ready and self.state != "off" and self.fix:
            lines.append(f"     قدم بعدی: {self.fix}")
        if self.stale:
            lines.append("     برای آزمون تازه: /doctor")
        return "\n".join(lines)

    def as_check(self) -> Check:
        """The report line, with the state's own icon and its own paragraph."""
        status: Status = "ok" if self.ready else "warn"
        return Check(
            FALLBACK_CHECK_NAME,
            status,
            self.detail(),
            icon=self.icon,
            section=True,
        )

    def stored(self) -> str:
        """Serialise for ``bot_state`` (one row, overwritten per run)."""
        dialect = self.dialect or "-"
        return f"{self.state}|{self.url}|{dialect}|{self.seconds if self.seconds else ''}|{self.reason}"

    @classmethod
    def parse(cls, value: str) -> FallbackHealth | None:
        """Read back a stored verdict; ``None`` when the row is not ours."""
        parts = value.split("|", 4)
        if len(parts) != 5 or parts[0] not in FALLBACK_TITLES:
            return None
        state, url, dialect, seconds, reason = parts
        return cls(
            state=state,
            url=url,
            dialect=None if dialect == "-" else dialect,
            seconds=float(seconds) if seconds else None,
            reason=reason,
            remembered=True,
        )


async def _remember_fallback(pool: asyncpg.Pool, health: FallbackHealth) -> None:
    """Keep the last verdict. Never raises: a report must not fail over its notes."""
    try:
        await database.set_state(pool, FALLBACK_STATE_KEY, health.stored())
    except Exception:
        logger.exception("could not remember the fallback verdict")


async def _recall_fallback(pool: asyncpg.Pool) -> FallbackHealth | None:
    try:
        value = await database.get_state(pool, FALLBACK_STATE_KEY)
    except Exception:
        logger.exception("could not read the remembered fallback verdict")
        return None
    return FallbackHealth.parse(value) if value else None


async def _probe_fallback(
    settings: Settings,
    cobalt: CobaltService | None,
    *,
    probe: bool,
    probe_url: str,
    pool: asyncpg.Pool | None = None,
) -> FallbackHealth:
    """Probe the fallback (or report what is known about it without a request)."""
    url = cobalt.base_url if cobalt is not None and cobalt.base_url else settings.cobalt_api_url
    if not url:
        return FallbackHealth("off")

    if not probe:
        # Offline: the config, a quarantine from earlier in this process, and what
        # the last run wrote down are the only facts available — all real ones.
        if cobalt is not None and cobalt.quarantined:
            return FallbackHealth(
                "quarantined", url, cobalt.dialect, reason=cobalt.quarantine_reason
            )
        if pool is not None and (remembered := await _recall_fallback(pool)):
            if remembered.about(url):
                return remembered
            # Saying nothing would read as "this instance is fine": name the fact
            # that the stored answer belongs to a different address.
            return FallbackHealth(
                "unknown",
                url,
                reason=(
                    f"آخرین نتیجه برای نمونهٔ دیگری بود ({remembered.url}) — "
                    "دربارهٔ این نمونه چیزی نمی‌گوید"
                ),
            )
        return FallbackHealth(
            "unknown", url, cobalt.dialect if cobalt else None, reason="تست زنده انجام نشد"
        )

    if cobalt is None:
        # A caller that owns no client cannot be given a live answer — and must not
        # get a fabricated one: say which fact is missing instead.
        return FallbackHealth(
            "unknown",
            url,
            reason="این اجرا کلاینت fallback را در اختیار ندارد (CLI بدون کلاینت یا تست)",
        )

    started = time.monotonic()
    try:
        media = await cobalt.resolve(probe_url, "video")
    except CobaltError as exc:
        seconds = time.monotonic() - started
        if exc.needs_auth:
            state = "auth"
        elif exc.youtube_session_missing:
            # It answered, and the answer is about YouTube's session rather than
            # about this link: a self-hosted instance needs cookies or a po-token
            # server for YouTube, the same way yt-dlp needs a logged-in jar. Since
            # YouTube is what most blocks are about, this is *the* state an operator
            # of an embedded instance will meet, and it needs its own fix.
            state = "youtube"
        elif exc.code in {"UNREACHABLE", "TIMEOUT"}:
            state = "unreachable"
        elif exc.instance:
            state = "quarantined" if cobalt.quarantined else "unreachable"
        else:
            # It answered — it just could not serve *this* link.
            state = "degraded"
        reason = f"{exc.code}: {exc.message}"
        if state == "unreachable":
            reason += _host_side_hint(url)
        return FallbackHealth(state, url, cobalt.dialect, seconds=seconds, reason=reason)
    except Exception as exc:  # noqa: BLE001 — a crash here is still a diagnosis
        return FallbackHealth(
            "unreachable",
            url,
            cobalt.dialect,
            reason=f"{type(exc).__name__}: {exc}{_host_side_hint(url)}",
        )

    logger.debug("fallback probe resolved %s in %.1fs", media.url[:60], time.monotonic() - started)
    return FallbackHealth(
        "ready", url, cobalt.dialect, seconds=time.monotonic() - started
    )


async def fallback_health(
    settings: Settings,
    cobalt: CobaltService | None,
    *,
    probe: bool,
    probe_url: str = DEFAULT_PROBE_URL,
    pool: asyncpg.Pool | None = None,
) -> FallbackHealth:
    """What the fallback engine is doing — for the doctor *and* for ``/blocks``.

    Two kinds of evidence, kept apart because they answer different questions: the
    probe (``probe=True``) says whether the instance would serve a link right now;
    the recorded use says whether it did, the last time a real blocked link needed
    it. A net can be 🟢 and still have let the last three links through on its own
    failure, so a report that only probes is a report that can look healthy while
    the users are not being served. ``probe=False`` never spends a request.
    """
    health = await _probe_fallback(
        settings, cobalt, probe=probe, probe_url=probe_url, pool=pool
    )
    if pool is not None and (use := await last_use(pool)) is not None:
        health = replace(health, use=use)
    if cobalt is not None:
        # Read from the *client*, not from the probe: with a pool, the interesting
        # news is usually about a node the probe never touched (the embedded one
        # cannot serve YouTube, so the second node took the link — that is a
        # failover an admin has to know about, and nothing in the verdict says it).
        health = replace(health, nodes=cobalt.node_states())
    return health


def _pool_line(nodes: tuple[CobaltNodeState, ...]) -> str:
    """The pool in one line — only when there is more than one instance to explain.

    A single instance is already named by the paragraph above, and repeating the
    address would read as two different facts. With a pool, the address an operator
    configured is often *not* the one that ends up serving a blocked link, so which
    node is out, and which one is taking the work, is the entire question.
    """
    if len(nodes) < 2:
        return ""
    parts: list[str] = []
    for node in nodes:
        marks = [node.dialect] if node.dialect else []
        if node.active:
            marks.append("فعال")
        if node.quarantined:
            marks.append("🟡 کنار گذاشته")
        parts.append(node.url + (f" ({' • '.join(marks)})" if marks else ""))
    return f"     🔁 نمونه‌ها ({len(nodes)}): " + " | ".join(parts)


def _host_side_hint(url: str) -> str:
    """The one line that turns "unreachable" into "you are on the wrong machine".

    Built from the same map the translation itself uses, so the address named here
    is the address this process would actually reach — for any helper, not just the
    fallback.
    """
    host = (urlparse(url).hostname or "").lower()
    port = COMPOSE_LOOPBACK_PORTS.get(host)
    if port is None or in_container():
        return ""
    setting = COMPOSE_SERVICE_ENV.get(host)
    return (
        f"؛ «{host}» فقط داخل شبکهٔ compose حل می‌شود — از روی هاست، همین سرویس روی "
        f"http://127.0.0.1:{port} است"
        + (f" ({setting})" if setting else "")
    )


def _fallback_note(health: FallbackHealth, probe_error: ExtractionError | None) -> str:
    """What the fallback means for the user *right now* — only when it matters.

    Two things are worth saying, and only after the primary path has actually been
    refused: that users are being served anyway, or that they are not (because the
    net has a hole too). In a healthy report this would be noise.
    """
    if probe_error is None or probe_error.code not in BLOCK_EXTRACTION_CODES:
        return ""
    if health.ready:
        return (
            "ℹ️ همین حالا لینک‌های بلاک‌شده از موتور جایگزین دانلود می‌شوند، پس کاربران بلاک "
            "نمی‌مانند — ولی رفع مسیر اصلی، مصرف کوبالت را کم می‌کند."
        )
    if health.state in {"off", "unknown"}:
        return ""
    if health.state == "youtube":
        return (
            f"⚠️ و موتور جایگزین برای یوتیوب {health.icon} سشن/کوکی ندارد، پس لینک‌های "
            "یوتیوب بلاک‌شده از آن هم رد نمی‌شوند (بقیهٔ سایت‌ها سالماند) — "
            f"{health.fix}"
        )
    return f"⚠️ و موتور جایگزین هم آماده نیست ({health.icon} {health.title}) — کاربران بلاک می‌مانند."


async def http_reachable(base_url: str, timeout: float = 5.0) -> bool:
    """Does a helper server (Bot API / PO-token provider) answer on this network?"""
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(base_url) as response:
                return response.status < 500
    except Exception:
        return False


async def _fetch_json(url: str, timeout: float = 5.0) -> tuple[int, object]:
    """GET a helper endpoint and read its JSON body, if it has one.

    The *status* is half the answer: the session server replies ``503`` with a
    plain-text "not yet generated" while its browser is still working, and that is
    news about a server that is very much alive. A port nobody listens on is the
    other half — ``0``, which no HTTP response can be mistaken for.
    """
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(probe_url(url)) as response:
                try:
                    payload = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 — a non-JSON body is still an answer
                    payload = None
                return response.status, payload
    except Exception:  # noqa: BLE001 — nothing listening is exactly what we report
        return 0, None


async def probe_pot_provider(url: str, timeout: float = 5.0) -> PotProvider:
    """Ask the PO-token provider whether it is up, and which version it speaks.

    ``/ping`` is also what yt-dlp's own plugin calls before it trusts the server, so
    this is the same question the download path asks — including the version, which
    decides whether the plugin accepts the server at all.
    """
    status, payload = await _fetch_json(f"{url}{POT_PING_PATH}", timeout)
    if status == 0:
        return PotProvider(reachable=False, error="پاسخ نمی‌دهد")
    version = payload.get("version") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(version, str) or not version:
        return PotProvider(reachable=True, error=f"پاسخ HTTP {status}")
    return PotProvider(reachable=True, version=version)


async def probe_session_server(url: str, timeout: float = 5.0) -> SessionServer:
    """Ask the session server for the token cobalt would load, without waiting.

    ``/token`` answers immediately either way (the browser is what takes minutes),
    so a probe never blocks on a generation in progress.
    """
    status, payload = await _fetch_json(f"{url}{SESSION_TOKEN_PATH}", timeout)
    if status == 0:
        return SessionServer(reachable=False, error="پاسخ نمی‌دهد")
    if status == 503:
        return SessionServer(
            reachable=True,
            error="توکن هنوز ساخته نشده — مرورگر در حال تولید است",
        )
    if status != 200:
        return SessionServer(reachable=True, error=f"پاسخ HTTP {status}")
    data = payload if isinstance(payload, dict) else {}
    token = data.get("potoken")
    visitor = data.get("visitor_data")
    if not isinstance(token, str) or not token or not isinstance(visitor, str) or not visitor:
        return SessionServer(
            reachable=True, error="پاسخ داد ولی potoken/visitor_data ندارد (نسخهٔ ناسازگار؟)"
        )
    return SessionServer(
        reachable=True,
        ready=True,
        age=_token_age(data.get("updated")),
        token_length=len(token),
    )


def _token_age(updated: object) -> float | None:
    """Seconds since the server stamped its token, whatever unit it stamped it in.

    The reference generator writes seconds (``int(time.time())``); a server that
    writes milliseconds would otherwise read as a token from the year 57000.
    """
    if not isinstance(updated, (int, float)) or not updated:
        return None
    seconds = updated / 1000 if updated > 1e11 else float(updated)
    age = time.time() - seconds
    return age if age >= 0 else None


def _cookie_check(settings: Settings, extractor: ExtractorService) -> Check:
    path = settings.cookie_file
    if path is None or not extractor.using_cookies:
        if path is None:
            return Check("کوکی", "warn", "COOKIE_FILE تنظیم نشده")
        if path.is_dir():
            # The Docker bind-mount trap, named so it is not mistaken for a
            # malformed jar: nothing was ever written here.
            return Check(
                "کوکی",
                "fail",
                f"{path} — پوشه است، نه فایل (فایل میزبان mount پیدا نشد)",
            )
        return Check("کوکی", "fail", f"{path} — قابل خواندن نیست")
    missing = missing_youtube_login_cookies(path)
    if missing:
        return Check("کوکی", "warn", f"{path} — لاگین یوتیوب ندارد (کم: {', '.join(missing)})")
    return Check("کوکی", "ok", f"{path} — لاگین یوتیوب کامل است")


def describe_age(seconds: float) -> str:
    """When something happened, the way an operator asks about it.

    Public because the cookie watcher phrases its alerts the same way.
    """
    if seconds < 90:
        return "همین حالا"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} دقیقه پیش"
    hours = minutes / 60
    if hours < 36:
        return f"{hours:.0f} ساعت پیش"
    return f"{hours / 24:.0f} روز پیش"


def mount_wording(state: CookieJarState) -> str:
    """Where the jar comes from, in one clause (shared with boot_check)."""
    if state.mount is None:
        # A Windows/macOS host run: no mount table to read, just a file.
        return "فایل روی دیسک میزبان (بدون mount)" if state.writable else "فایل فقط-خواندنی"
    mount = state.mount
    access = "فقط-خواندنی" if not state.writable else "قابل نوشتن"
    origin = f"mount {mount.point} ({mount.filesystem}، {access})"
    if mount.root not in ("", "/"):
        # A subtree mount is a bind: this is the jar being handed in from outside,
        # so show which host directory it came from (that is what to edit).
        origin += f"، مسیر میزبان: {mount.root}"
    return origin


def _cookie_storage_check(extractor: ExtractorService) -> Check:
    """Where the jar lives, when it was exported, and whether it is in use.

    Answers the question unit tests cannot: "I exported new cookies — is the
    running bot using them?".
    """
    state = extractor.cookie_jar_state()
    if state.path is None:
        return Check("منبع کوکی", "warn", "COOKIE_FILE تنظیم نشده")
    if state.kind == "directory":
        return Check(
            "منبع کوکی",
            "fail",
            f"{state.path} — پوشه است، نه فایل (mount اشتباه یا باقی‌ماندهٔ mount قبلی)",
        )
    if state.kind == "missing":
        return Check(
            "منبع کوکی",
            "warn",
            f"{state.path} — فایل نیست؛ ربات بدون کوکی کار می‌کند (فقط سایت‌های لاگین‌دار محدود می‌شوند)",
        )

    exported = f"اکسپورت {describe_age(time.time() - state.exported_at)}" if state.exported_at else ""
    size = f"{state.cookie_count} کوکی" if state.cookie_count else "بدون کوکی قابل‌خواندن"
    detail = f"{state.path} — {mount_wording(state)}، {size}" + (f"، {exported}" if exported else "")

    if state.in_sync is False:
        # Not a failure: the copy is refreshed on every download, so this is the
        # normal state right after an export.
        return Check(
            "منبع کوکی",
            "warn",
            f"{detail}؛ اکسپورت تازه‌تر از کپی است و در دانلود بعدی برداشته می‌شود (بدون ری‌استارت)",
        )
    if state.in_sync is None:
        return Check("منبع کوکی", "ok", f"{detail}؛ این اجرا هنوز کوکی را نخوانده — دانلود بعدی می‌خواند")
    return Check("منبع کوکی", "ok", f"{detail}؛ از همین اکسپورت استفاده می‌شود")


def _provider_check(
    settings: Settings, provider: PotProvider | None, plugin_version: str | None = None
) -> Check:
    """yt-dlp's PO-token provider: configured, installed, answering — and compatible.

    The version is not decoration. The plugin *rejects* a server one major version
    away, and the compose image is tagged ``:latest``, so a provider that answers
    perfectly can still cost every download its token. Two numbers, one line.
    """
    if not settings.wants_pot_provider:
        return Check(
            POT_CHECK_NAME,
            "warn",
            "خاموش (YTDLP_POT_PROVIDER_URL خالی) — برگرداندن مقدار پیش‌فرض کافی است: "
            "http://pot-provider:4416",
        )
    if not pot_plugin_installed():
        return Check(POT_CHECK_NAME, "fail", "پلاگین bgutil نصب نیست (requirements.txt)")
    if provider is None or not provider.reachable:
        return Check(
            POT_CHECK_NAME,
            "fail",
            f"{settings.ytdlp_pot_provider_url} پاسخ نمی‌دهد — دانلودها بدون توکن ادامه پیدا "
            "می‌کنند (yt-dlp خودش provider را رد می‌کند، پس این «شانس کمتر» است نه «خرابی»)؛ "
            "docker compose up -d pot-provider"
            + _host_side_hint(settings.ytdlp_pot_provider_url),
        )
    if provider.error:
        return Check(
            POT_CHECK_NAME, "fail", f"{settings.ytdlp_pot_provider_url} — {provider.error}"
        )
    if plugin_version and _major(provider.version) != _major(plugin_version):
        return Check(
            POT_CHECK_NAME,
            "warn",
            f"{settings.ytdlp_pot_provider_url} — نسخهٔ سرور v{provider.version} با پلاگین این "
            f"ایمیج v{plugin_version} ناهماهمانگ (major متفاوت) و پلاگین آن را *رد می‌کند*: "
            "توکنی برای هیچ دانلودی ساخته نمی‌شود. image را هم‌نسخه کنید "
            "(bgutil-ytdlp-pot-provider در requirements.txt)",
        )
    return Check(POT_CHECK_NAME, "ok", f"{settings.ytdlp_pot_provider_url} — v{provider.version}")


def _major(version: str) -> str:
    return version.split(".", 1)[0]


def _session_server_check(settings: Settings, server: SessionServer | None) -> Check:
    """The fallback engine's YouTube session: the one route that needs no login.

    Cobalt reads the token from this server itself; this check exists so an admin
    learns that the route is dead here instead of discovering it from a user whose
    YouTube link fell through twice.
    """
    if not settings.wants_session_server:
        return Check(
            SESSION_CHECK_NAME,
            "warn",
            "خاموش (YOUTUBE_SESSION_SERVER خالی) — مسیر بدون‌لاگین برای یوتیوب کار نمی‌کند",
        )
    where = settings.youtube_session_server
    if server is None or not server.reachable:
        return Check(
            SESSION_CHECK_NAME,
            "fail",
            f"{where} پاسخ نمی‌دهد — کوبالت هم همین را می‌خواند، پس یوتیوب را بدون سشن "
            "می‌بیند (docker compose up -d yt-session-generator)"
            + _host_side_hint(where),
            icon="🎫",
        )
    if not server.ready:
        return Check(
            SESSION_CHECK_NAME,
            "warn",
            f"{where} — {server.error}. تولید توکن در این سرور با یک مرورگر واقعی انجام "
            "می‌شود و گاهی چند دقیقه می‌گیرد؛ اگر همین‌طور ماند، لاگش می‌گوید چرا "
            "(docker compose logs yt-session-generator) — تا آن موقع لینک‌های یوتیوب "
            "از مسیر جایگزین هم رد نمی‌شوند",
            icon="🎫",
        )
    facts = "توکن آماده"
    if server.age is not None:
        facts += f"، ساخته‌شده {describe_age(server.age)} پیش"
    if server.short:
        facts += f"، ولی کوتاه ({server.token_length} کاراکتر — کوبالت خودش هشدار می‌دهد)"
    return Check(SESSION_CHECK_NAME, "ok", f"{where} — {facts}؛ {SESSION_RELOAD_NOTE}", icon="🎫")


def _runtime_check(extractor: ExtractorService) -> Check:
    name = extractor.js_runtime_name
    if name == "none":
        return Check("JS runtime", "warn", "پیدا نشد — کیفیتهای یوتیوب ممکن است ناقص باشد")
    return Check("JS runtime", "ok", name)


def _base_checks(
    settings: Settings,
    extractor: ExtractorService,
    provider: PotProvider | None,
    plugin_version: str | None,
    server: SessionServer | None,
) -> list[Check]:
    return [
        Check(
            "ffmpeg",
            "ok" if extractor.ffmpeg_available else "warn",
            "موجود" if extractor.ffmpeg_available else "نصب نیست — MP3 و ادغام ویدیو کار نمیکند",
        ),
        _cookie_check(settings, extractor),
        _cookie_storage_check(extractor),
        _runtime_check(extractor),
        _provider_check(settings, provider, plugin_version),
        _session_server_check(settings, server),
    ]


def _verdict(
    checks: list[Check],
    *,
    probe_status: Status,
    probe_error: ExtractionError | None,
    provider_reachable: bool | None,
    settings: Settings,
) -> tuple[str, str]:
    """Turn the collected evidence into one verdict and one next step."""
    cookies = next(check for check in checks if check.name == "کوکی")
    provider = next(check for check in checks if check.name == "PO token")

    if probe_error is None:
        if probe_status == "ok":
            weaker = [check.name for check in checks if check.status == "warn"]
            return (
                "✅ مسیر دانلود یوتیوب سالم است.",
                f"اما این‌ها جای رسیدگی دارند: {'، '.join(weaker)}"
                if weaker
                else "چیزی برای درستکردن نمانده.",
            )
        return ("ℹ️ تست زنده انجام نشد.", "برای اطمینان کامل بدون --no-probe اجرا کنید.")

    if probe_error.code == "SESSION_STALE":
        # Not a block: YouTube accepted the session but refused the request. The
        # usual causes are a rotated login, an unusable visitor binding, or a
        # missing PO token — all cheaper to try than a proxy.
        retry_note = (
            f"ربات خودش تا {settings.extractor_retry_attempts} بار دیگر با فاصلهٔ نمایی تلاش "
            "میکند (این گزارش یکبار و بدون retry اجرا میشود). "
            if settings.extractor_retry_attempts
            else ""
        )
        if not settings.wants_pot_provider:
            return (
                "⛔️ سشن پذیرفته شد ولی یوتیوب درخواست را نپذیرفت.",
                retry_note
                + "provider توکن خاموش است — همان چیزی که تکرارها را نجات می‌دهد: "
                "YTDLP_POT_PROVIDER_URL را به پیش‌فرض برگردانید (http://pot-provider:4416).",
            )
        if provider.status == "fail":
            return (
                "⛔️ سشن و provider ناهماهماند.",
                retry_note
                + "دسترس‌بودن provider را درست کنید (docker compose up -d pot-provider)؛ "
                "بدون توکن، یوتیوب درخواست را نیمه‌کاره رد می‌کند.",
            )
        if cookies.status != "ok":
            return (
                "⛔️ سشن کهنه است.",
                retry_note
                +                "کوکی را تازه کنید: یک بار در مرورگر وارد یوتیوب شوید و "
                "python scripts/export_cookies.py را دوباره بزنید (فایل mount شده است و "
                "در دانلود بعدی خوانده میشود).",
            )
        return (
            "⛔️ همهچیز سر جایش است و یوتیوب باز درخواست را رد میکند.",
            (retry_note or "یک تلاش دوباره معمولاً کار میکند (این خطا گذراست). ")
            + "اگر ماند، کوکی را تازه کنید و در نهایت YTDLP_PROXY را روی یک IP تمیز بگذارید.",
        )

    if probe_error.code != "EXTRACTOR_BLOCKED":
        return (
            f"⛔️ یوتیوب این لینک را نداد ({probe_error.code}).",
            probe_error.message,
        )

    # The bot check. Its causes are ordered by how often they are the real one.
    if cookies.status != "ok":
        return (
            "⛔️ یوتیوب درخواست را ناشناس میبیند، پس بلاک میکند.",
            "یک بار در مرورگر وارد یوتیوب شوید و کوکی را دوباره بگیرید "
            "(python scripts/export_cookies.py --browser chrome) — کوکی mount شده و در دانلود "
            "بعدی خوانده میشود، پس ریبیلد/ریاستارت لازم نیست. "
            "کوکی ناقص با IP قابلمنع فرق دارد.",
        )
    if not settings.wants_pot_provider:
        return (
            "⛔️ لاگین درست است ولی یوتیوب همچنان بلاک میکند.",
            "provider توکن خاموش است — تنها مسیری که به لاگین نیاز ندارد: "
            "YTDLP_POT_PROVIDER_URL را به پیش‌فرض برگردانید (http://pot-provider:4416).",
        )
    if provider.status == "fail" or provider_reachable is False:
        return (
            "⛔️ provider تنظیم شده ولی در دسترس نیست.",
            "کانتینرش را بالا بیاورید (docker compose up -d pot-provider) یا "
            "YTDLP_POT_PROVIDER_URL را خالی کنید تا دانلود بی‌توکن ادامه پیدا کند.",
        )
    if settings.ytdlp_proxy:
        return (
            "⛔️ لاگین، توکن و پروکسی هر سه تنظیماند و باز بلاک میشوید.",
            "پروکسی را عوض کنید: IP فعلی حتی برای کاربر لاگینشده هم پذیرفته نمیشود.",
        )
    return (
        "⛔️ لاگین و توکن درستاند، پس خود IP پذیرفته نمیشود.",
        "YTDLP_PROXY را روی یک IP تمیز بگذارید (socks5://user:pass@host:1080) — "
        "تنها راهحل مطمئن برای IP مسدود.",
    )


async def run_youtube_doctor(
    settings: Settings,
    extractor: ExtractorService,
    *,
    cobalt: CobaltService | None = None,
    pool: asyncpg.Pool | None = None,
    probe: bool = True,
    probe_url: str = DEFAULT_PROBE_URL,
) -> DoctorReport:
    """Run every check in order and return one verdict.

    ``probe=False`` keeps it offline — no extraction, no request to YouTube (useful
    in tests and on hosts with no outbound access). The two *helper* servers are
    still asked: they are local, answering them says whether the routes that need no
    login are even switched on, and a refused connection costs nothing.

    ``cobalt`` is the *running* fallback client: passing it means the report shows
    its live state (including a quarantine from earlier in this process) instead of
    a fresh opinion, which is what an operator asking "what is happening now"
    wants. Without one — a CLI run with no client of its own — the section still
    appears, and says which facts were not available rather than inventing any.

    ``pool`` lets the last verdict be written down and read back, so an offline run
    (or one after a restart) still answers "was the safety net working?".
    """
    provider: PotProvider | None = None
    if settings.wants_pot_provider:
        provider = await probe_pot_provider(settings.ytdlp_pot_provider_url)
    provider_reachable = None if provider is None else provider.reachable
    server: SessionServer | None = None
    if settings.wants_session_server:
        server = await probe_session_server(settings.youtube_session_server)
    checks = _base_checks(settings, extractor, provider, pot_plugin_version(), server)

    if settings.cookies_from_browser:
        checks.append(
            Check(
                "کوکی مرورگر",
                "ok" if extractor.using_browser_cookies else "warn",
                settings.cookies_from_browser
                if extractor.using_browser_cookies
                else f"{settings.cookies_from_browser} — در دسترس نیست، نادیده گرفته شد",
            )
        )

    probe_error: ExtractionError | None = None
    probe_status: Status = "warn"
    if probe:
        try:
            info = await extractor.extract(probe_url)
        except ExtractionError as exc:
            probe_error = exc
            probe_status = "fail"
            checks.append(Check("تست زنده", "fail", f"{exc.code}: {exc.message}"))
        except Exception as exc:  # noqa: BLE001 — a crash here is still a diagnosis
            probe_error = ExtractionError("GENERAL", f"{type(exc).__name__}: {exc}")
            probe_status = "fail"
            checks.append(Check("تست زنده", "fail", str(probe_error.message)[:120]))
        else:
            probe_status = "ok"
            seconds = f" ({info.duration}s)" if info.duration else ""
            checks.append(Check("تست زنده", "ok", f"{info.platform}: {info.title[:60]}{seconds}"))

    # The fallback engine, last: it is what happens *after* the chain above says
    # no, and its answer can change what the user experiences without changing
    # anything above it.
    health = await fallback_health(
        settings, cobalt, probe=probe, probe_url=probe_url, pool=pool
    )
    checks.append(health.as_check())
    if probe and pool is not None:
        await _remember_fallback(pool, health)

    # The fallback's own credentials: a cookie file generated from the same jar,
    # and — since cobalt reads it once, at startup — whether the running instance
    # has the version on disk.
    checks.append(
        await cobalt_cookie_check(settings, cobalt, jar_path=extractor.cookie_file, probe=probe)
    )

    verdict, next_step = _verdict(
        checks,
        probe_status=probe_status,
        probe_error=probe_error,
        provider_reachable=provider_reachable,
        settings=settings,
    )
    if note := _fallback_note(health, probe_error):
        next_step = f"{next_step} {note}"
    if not probe:
        verdict = "ℹ️ تست زنده انجام نشد."
        next_step = f"برای اطمینان کامل بدون --no-probe اجرا کنید ({probe_url})."

    return DoctorReport(checks=tuple(checks), verdict=verdict, next_step=next_step)
