"""The guided fix for the failure that keeps masquerading as a blocked IP.

A jar without ``LOGIN_INFO`` plus a ``SAPISID`` cookie sends every YouTube request
anonymously, and YouTube answers *"Sign in to confirm you're not a bot"* — which
reads exactly like a flagged host. The fix is mechanical, but it has two traps: the
browser has to be signed in to YouTube, and the export has to keep the HTTP-only
rows (``LOGIN_INFO`` is one of them, and "export cookies" extensions drop them by
default).

This module holds the deciding parts — what the jar is missing, which profiles this
machine even has, what to do next in words, and whether the login actually landed —
so the CLI wizard (``scripts/fix_login.py``) and ``/fixlogin`` in Telegram tell the
same story about the same jar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from core.utils import escape_html
from services.cookie_watch import verify_export
from services.extractor import (
    SUPPORTED_BROWSERS,
    CookieJarState,
    ExtractorService,
    app_bound_encryption_active,
    browser_profile_paths,
    browser_profile_reachable,
)

logger = logging.getLogger(__name__)

#: The browsers we can point at by name, i.e. the ones whose profile layout we
#: know well enough to find (``SUPPORTED_BROWSERS`` also has Safari, which is
#: macOS-only and has no per-profile path to check).
KNOWN_BROWSERS: tuple[str, ...] = (
    "chrome",
    "edge",
    "brave",
    "chromium",
    "vivaldi",
    "opera",
    "whale",
    "firefox",
)

#: The two ways a "successful" export still has no login. Ordered by how often
#: each is the answer, because the operator reads them in order.
LOGIN_TRAPS: tuple[str, ...] = (
    "مرورگر وارد یوتیوب نبوده — در همان مرورگر youtube.com را باز کنید و مطمئن شوید "
    "اکانت بالای صفحه دیده می‌شود، بعد دوباره اکسپورت بگیرید.",
    "اکسپورت ردیف‌های HttpOnly را نداده — LOGIN_INFO و SAPISID فقط HttpOnly هستند و "
    "بیشتر افزونه‌های «export cookies» به‌صورت پیش‌فرض حذفشان می‌کنند. گزینهٔ "
    "include HttpOnly را روشن کنید (یا از scripts/export_cookies.py استفاده کنید، "
    "که این ردیف‌ها را برمی‌دارد).",
)


@dataclass(frozen=True)
class JarDiagnosis:
    """What the jar on disk can and cannot do."""

    path: Path | None
    kind: str
    cookies: int
    missing_login: tuple[str, ...]

    @property
    def usable(self) -> bool:
        return self.kind == "ok"

    @property
    def signed_in(self) -> bool:
        """The jar both reads *and* carries a YouTube login."""
        return self.usable and not self.missing_login

    @property
    def summary(self) -> str:
        if self.path is None:
            return "هیچ جاری تنظیم نشده (COOKIE_FILE خالی است)"
        if not self.usable:
            return f"جار خوانده نمی‌شود ({self.kind}): {self.path}"
        if self.signed_in:
            return f"جار سالم و لاگین‌شده ({self.cookies} کوکی): {self.path}"
        return (
            f"جار خوانده می‌شود ({self.cookies} کوکی) ولی لاگین یوتیوب ندارد — "
            f"کم: {', '.join(self.missing_login)}"
        )


@dataclass(frozen=True)
class ProfileCandidate:
    """A browser profile that exists on this machine — readable or not."""

    spec: str
    browser: str
    paths: tuple[Path, ...]
    #: Windows App-Bound Encryption: the profile is here, the cookie database is
    #: locked with the browser's own key, and no outside tool can open it.
    locked: bool = False

    @property
    def found(self) -> Path | None:
        """The path that actually exists (``None`` when it only *might* be there)."""
        return next((path for path in self.paths if path.exists()), None)

    @property
    def label(self) -> str:
        where = f"{self.browser} — {self.found}" if self.found else self.browser
        if self.locked:
            return f"{where} (قفل App-Bound ویندوز — با yt-dlp خوانده نمی‌شود)"
        return where


def diagnose_from_state(state: CookieJarState) -> JarDiagnosis:
    """Turn the extractor's jar state into the wizard's diagnosis.

    The state is trusted as-is: the extractor computes ``missing_login`` from the
    file itself, and re-reading it here would be a second opinion nobody asked for
    (and a different answer after the file changed under us).
    """
    return JarDiagnosis(
        path=state.path,
        kind=state.kind,
        cookies=state.cookie_count,
        missing_login=tuple(state.missing_login),
    )


def diagnose(extractor: ExtractorService) -> JarDiagnosis:
    """Read the jar through the same service that reads it in production."""
    return diagnose_from_state(extractor.cookie_jar_state())


def candidates() -> tuple[ProfileCandidate, ...]:
    """The browser profiles this machine actually has (best effort).

    A browser whose layout we know but whose profile is absent is not offered: on
    a plain container this returns nothing, which is the honest answer — there is
    no browser here to read, and the export belongs where the browser is.
    """
    found: list[ProfileCandidate] = []
    for browser in KNOWN_BROWSERS:
        if browser not in SUPPORTED_BROWSERS:
            continue
        try:
            if not browser_profile_reachable(browser):
                continue
            paths = browser_profile_paths(browser)
        except Exception:  # noqa: BLE001 — a probe of the filesystem, never fatal
            logger.debug("could not look for the %s profile", browser, exc_info=True)
            continue
        found.append(
            ProfileCandidate(
                spec=browser,
                browser=browser,
                paths=paths,
                locked=app_bound_encryption_active(browser),
            )
        )
    return tuple(found)


def next_steps(diagnosis: JarDiagnosis, *, have_profile: bool) -> tuple[str, ...]:
    """What to do about *this* jar, in words — the one list both front ends print."""
    if diagnosis.signed_in:
        return (
            "لاگین یوتیوب کامل است، پس مشکل از جار نیست.",
            "اگر باز بلاک می‌شوید علت IP است: یک YTDLP_PROXY روی IP تمیز، یا provider "
            "توکن (docker compose --profile pot up -d pot-provider).",
            "برای حکم نهایی: /doctor",
        )
    if not diagnosis.usable:
        return (
            "اول یک جار سالم لازم است: روی ماشینی که مرورگر لاگین‌شده دارد "
            "python scripts/export_cookies.py را بزنید (جارِ mountشده خودش برداشته می‌شود).",
            "بعد همین را دوباره امتحان کنید تا لاگین هم بررسی شود.",
        )
    steps = [
        "۱) در همان مرورگری که کوکی را از آن می‌گیرید وارد youtube.com شوید و مطمئن "
        "شوید اکانت بالای صفحه دیده می‌شود.",
        "۲) روی همان ماشین python scripts/fix_login.py را بزنید — خودش پروفایل را پیدا "
        "می‌کند، اکسپورت می‌گیرد، بررسی می‌کند LOGIN_INFO رسیده یا نه و نتیجه را به "
        "ادمین‌ها هم می‌گوید.",
    ]
    if not have_profile:
        steps.append(
            "از این اجرا مرورگری دیده نشد (کانتینر/سرور)، پس قدم ۲ باید روی همان "
            "ماشینی اجرا شود که مرورگر دارد."
        )
    steps.append(
        "اگر مرورگر در دسترس نیست: با افزونه‌ای که include HttpOnly را دارد اکسپورت "
        "بگیرید و cookies.txt را جایگزین کنید؛ بعد /doctor."
    )
    return tuple(steps)


def render_fixlogin(
    diagnosis: JarDiagnosis, profiles: tuple[ProfileCandidate, ...] = ()
) -> str:
    """The ``/fixlogin`` message: state, then the steps, then the traps."""
    lines = [
        "🔧 <b>راهنمای ورود واقعی یوتیوب</b>",
        "",
        f"وضعیت: {escape_html(diagnosis.summary)}",
    ]
    if profiles:
        listed = "، ".join(escape_html(profile.spec) for profile in profiles)
        lines.append(f"پروفایل‌های پیدا‌شده روی این ماشین: <code>{listed}</code>")
    lines.append("")
    lines += [escape_html(step) for step in next_steps(diagnosis, have_profile=bool(profiles))]
    if diagnosis.usable and diagnosis.missing_login:
        lines.append("")
        lines.append("<b>دو تلهٔ همیشگی:</b>")
        lines += [f"• {escape_html(trap)}" for trap in LOGIN_TRAPS]
    return "\n".join(lines)


async def probe(extractor: ExtractorService, state: CookieJarState | None = None) -> str:
    """The live verdict for this jar (one line, never raises)."""
    return await verify_export(extractor, state or extractor.cookie_jar_state())
