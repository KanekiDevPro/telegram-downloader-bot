"""Tell the admins when a fresh cookie jar is waiting to be picked up.

A replaced ``cookies.txt`` takes effect on the next download — that is what makes
it painless, and also silent. Nothing says whether the *running* bot has actually
moved to the new jar, and the export mistake that keeps biting (a jar with no
YouTube login) is invisible until a download fails in a way that looks like a
blocked IP. ``/doctor`` answers this, but only when somebody asks it.

This watcher asks for you: it watches the jar, and the first time it sees an
export the running bot has not loaded, it messages every admin once — with the
mount it came from, how old it is, and whether it would even sign YouTube in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import asyncpg
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import Settings
from core.utils import escape_html
from services import cobalt_cookies, preflight, telemetry
from services.doctor import (
    COBALT_RESTART_FIX,
    DEFAULT_PROBE_URL,
    describe_age,
    mount_wording,
)
from services.extractor import CookieJarState, ExtractionError, ExtractorService

logger = logging.getLogger(__name__)

#: How often the jar is re-checked. The window this closes is normally seconds
#: (the next download), so the poll only has to be fast enough to be useful.
DEFAULT_INTERVAL_S = 60.0

#: What identifies an export. The extractor decides a copy is outdated from the
#: same pair, so "the stamp changed" means the same thing to both of us.
ExportStamp = tuple[float, int]

#: The alert's "check it now" button. The alert is not a dead end: one tap runs
#: ``/doctor`` and edits that message with the verdict. Single source of truth
#: for the handler in ``handlers/admin.py`` and the button below.
DOCTOR_CALLBACK = "cookie:doctor"

#: The alert's second action: read the browser profile and replace the jar right
#: now (the same thing ``/refresh`` does), then edit the alert with the outcome.
REFRESH_CALLBACK = "cookie:refresh"


def export_stamp(state: CookieJarState) -> ExportStamp | None:
    """Identify the jar on disk, or ``None`` when there is no jar file at all.

    ``None`` (absent, or the directory Docker leaves where a missing
    bind-mounted file was expected) is deliberately *not* an export: there is
    nothing to pick up, and removing a jar is a choice, not a symptom.
    """
    if state.exported_at is None or state.size_bytes is None:
        return None
    return (state.exported_at, state.size_bytes)


def _login_warning(state: CookieJarState) -> str:
    """The one line that turns a "meh" alert into an actionable one."""
    if not state.missing_login:
        return ""
    missing = escape_html(", ".join(state.missing_login))
    return (
        f"\n\n⚠️ ولی این اکسپورت لاگین یوتیوب ندارد (کم: <code>{missing}</code>) — "
        "تا وقتی این‌طور است یوتیوب ربات را ناشناس می‌بیند، پس بلاک‌شدن شبیه "
        "«IP مسدود» دیده می‌شود ولی نیست. یک بار در مرورگر وارد یوتیوب شوید و "
        "<code>python scripts/export_cookies.py</code> را دوباره بزنید."
    )


def fresh_alert(state: CookieJarState) -> str:
    """``cookies.txt`` was replaced; the running bot is still on the old one."""
    age = describe_age(time.time() - state.exported_at) if state.exported_at else "نامشخص"
    if state.in_sync is False:
        # A copy exists and is older than the jar: downloads are still reading
        # the previous export until the next one starts.
        waiting = "ربات هنوز روی اکسپورت قبلی است"
    else:
        # Nothing was ever copied (no download has needed cookies yet, or the
        # jar only just appeared), so there is nothing to replace — the first
        # download that needs cookies takes the new jar.
        waiting = "این اجرا هنوز هیچ کوکی‌ای نخوانده"
    return (
        "🍪 <b>کوکی تازه اکسپورت شد، ولی ربات هنوز برنداشته</b>\n\n"
        f"فایل: <code>{escape_html(str(state.path))}</code>\n"
        f"منبع: {escape_html(mount_wording(state))}\n"
        f"جار: {state.cookie_count} کوکی، اکسپورت {age}\n"
        f"وضعیت: {waiting}\n\n"
        "👉 دانلود بعدی خودش این نسخه را برمی‌دارد؛ ری‌استارت یا ری‌بیلد لازم نیست."
        + _login_warning(state)
    )


def regression_alert(state: CookieJarState) -> str:
    """A jar that used to work stopped being a usable cookie jar."""
    if state.kind == "directory":
        why = "پوشه است، نه فایل (mount اشتباه یا بازماندهٔ mount قبلی)"
    else:
        why = "کوکی قابل‌خواندنی ندارد (اکسپورت ناقص یا فایل خالی‌شده)"
    return (
        "⚠️ <b>جار کوکی از کار افتاد</b>\n\n"
        f"فایل: <code>{escape_html(str(state.path))}</code>\n"
        f"منبع: {escape_html(mount_wording(state))}\n"
        f"مشکل: {why}\n\n"
        "💡 تا وقتی این‌طور است، دانلودهایی که لاگین می‌خواهند (یوتیوب و محتوای "
        "محدود) شکست می‌خورند. اکسپورت را دوباره بگیرید: "
        "<code>python scripts/export_cookies.py</code>"
    )


#: A metadata-only probe of one public video. The extractor's own timeout can be
#: minutes; an alert that arrives that late is worth less than one that says
#: "could not check".
PROBE_TIMEOUT_S = 45.0


@dataclass(frozen=True)
class CookieAlert:
    """What the admins should hear, plus what is needed to check it first."""

    kind: Literal["fresh", "regression"]
    text: str
    state: CookieJarState


async def verify_export(extractor: ExtractorService, state: CookieJarState) -> str:
    """Ask YouTube what it makes of *this* jar, in one line, with a cheap probe.

    Counting cookies cannot separate "signed in" from "signed out but plausible":
    the two jars look alike and the difference is everything. The only honest
    answer comes from the request that was failing, so the alert probes once, with
    a metadata-only extraction (``skip_download``) of the same public video the
    doctor uses — seconds and a few requests, no download.

    Never raises: an alert is more useful unverified than unsent.
    """
    try:
        info = await asyncio.wait_for(extractor.extract(DEFAULT_PROBE_URL), PROBE_TIMEOUT_S)
    except ExtractionError as exc:
        return _failed_verdict(exc, state)
    except (asyncio.TimeoutError, TimeoutError):
        return (
            f"❓ نشد با این اکسپورت تست کنم ({PROBE_TIMEOUT_S:.0f} ثانیه طول کشید) — "
            "/doctor حکم نهایی را می‌دهد."
        )
    except Exception as exc:
        return (
            f"❓ نشد با این اکسپورت تست کنم ({type(exc).__name__}) — "
            "/doctor حکم نهایی را می‌دهد."
        )
    # A request with this jar worked: whatever was refused before, this is news.
    preflight.clear_anonymous_refusal()
    return (
        f"✅ تست با همین اکسپورت: متادیتای «{escape_html(info.title[:60])}» خوانده شد — "
        "یوتیوب الان درخواست را رد نکرد."
    )


def _failed_verdict(error: ExtractionError, state: CookieJarState) -> str:
    """The probe failed; say which of the repeated failures this one is."""
    if error.code == "EXTRACTOR_BLOCKED":
        if state.missing_login:
            # Evidence for the gateway's preflight: anonymous requests are being
            # refused right now, so the next YouTube link can be told up front.
            preflight.note_anonymous_refusal()
            missing = escape_html(", ".join(state.missing_login))
            return (
                "⛔️ با همین اکسپورت هم یوتیوب رد کرد — همان لاگین ناقص "
                f"(<code>{missing}</code>) توضیحش می‌دهد، نه IP. اکسپورت را وقتی "
                "مرورگر وارد یوتیوب است بگیرید."
            )
        return (
            "⛔️ لاگین کامل است و یوتیوب باز رد می‌کند → مسئله IP است "
            "(YTDLP_PROXY یا PO token)؛ /doctor قدم بعدی را می‌گوید."
        )
    if error.code == "SESSION_STALE":
        return (
            "⚠️ با همین اکسپورت، یوتیوب سشن را کهنه دید — خطای گذراست؛ ربات خودش "
            "با فاصله دوباره تلاش می‌کند و معمولاً درست می‌شود."
        )
    return f"❓ تست با همین اکسپورت با خطای {error.code} تمام شد: {error.message}"


def alert_keyboard() -> InlineKeyboardMarkup:
    """What an alert leads to, from the chat: check it, or fix the jar itself."""
    builder = InlineKeyboardBuilder()
    builder.button(text="بررسی همین حالا", callback_data=DOCTOR_CALLBACK)
    builder.button(text="♻️ اکسپورت دوباره", callback_data=REFRESH_CALLBACK)
    return builder.as_markup()


class CookieJarWatcher:
    """One alert per export, for the admins who would otherwise never know.

    Stateful on purpose: the interesting event is a *change* under a running bot,
    so the startup jar is a baseline (never an alert — otherwise every restart
    with a stale copy would ping the admins) and each export is announced once.
    """

    def __init__(
        self, extractor: ExtractorService, *, interval_s: float = DEFAULT_INTERVAL_S
    ) -> None:
        self._extractor = extractor
        self._interval_s = interval_s
        self._primed = False
        self._announced: ExportStamp | None = None
        self._last_usable = False

    @property
    def interval_s(self) -> float:
        return self._interval_s

    @property
    def enabled(self) -> bool:
        """``COOKIE_WATCH_INTERVAL_S=0`` switches the watcher off."""
        return self._interval_s > 0

    def prime(self) -> CookieJarState:
        """Take the startup baseline; whatever is on disk now is not news."""
        state = self._extractor.cookie_jar_state()
        self._decide(state)
        return state

    @property
    def extractor(self) -> ExtractorService:
        """The jar's owner — the probe runs through the service that reads it."""
        return self._extractor

    def inspect(self, state: CookieJarState | None = None) -> CookieAlert | None:
        """The alert this jar deserves right now, or ``None``.

        ``state`` is injectable so the decision can be tested without a real
        jar; passing it does not skip the baseline (the first call never alerts).
        """
        return self._decide(self._extractor.cookie_jar_state() if state is None else state)

    def _decide(self, state: CookieJarState) -> CookieAlert | None:
        stamp = export_stamp(state)
        usable = state.kind == "ok"
        kind: Literal["fresh", "regression"] | None = None
        if self._primed:
            if usable and stamp != self._announced and state.in_sync is not True:
                # A new export the running bot has not loaded — the exact window
                # this watcher exists for. The next download closes it silently,
                # which is why the check is "not loaded" and not "stale forever".
                kind = "fresh"
            elif not usable and self._last_usable and state.kind != "missing":
                # A jar that worked and stopped working. Removal stays quiet: it
                # is deliberate here, and the normal state of a fresh clone.
                kind = "regression"
        self._primed = True
        self._announced = stamp
        self._last_usable = usable
        if kind is None:
            return None
        logger.warning(
            "cookie jar changed under the running bot: %s (%s)",
            state.path,
            "fresh export waiting" if kind == "fresh" else "no longer usable",
        )
        text = fresh_alert(state) if kind == "fresh" else regression_alert(state)
        return CookieAlert(kind=kind, text=text, state=state)


async def notify_admins(
    bot: Bot,
    admin_ids: Iterable[int],
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    """Send one alert to every admin; one failure must not stop the others."""
    delivered = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                admin_id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
            delivered += 1
        except Exception:
            logger.exception("could not send the cookie-jar alert to admin %s", admin_id)
    if not delivered:
        logger.warning(
            "cookie-jar alert reached no admin — the jar state is in the log above; "
            "check ADMIN_IDS."
        )
    return delivered


def cobalt_cookie_note(settings: Settings | None, jar: Path | None) -> str:
    """The line that turns one export into *both* engines' login.

    Cobalt reads its cookie file once, at startup, so a fresh jar is not a fresh
    cobalt login until the instance is restarted — and nobody discovers that from
    a failed download, which is exactly what this sentence is for. Written only
    when the content actually changed: a restart that rewrites the same bytes is
    not news.
    """
    if settings is None:
        return ""
    state = cobalt_cookies.sync_from_jar(settings, jar_path=jar)
    if state.off or not state.usable or not state.written:
        return ""
    note = (
        f"\n\n🍪 همین کوکی‌ها برای موتور جایگزین هم نوشته شد ({state.cookie_count} کوکی)"
        " — کوبالت فایلش را فقط هنگام بالا آمدن می‌خواند:"
        f" <code>{COBALT_RESTART_FIX}</code>"
    )
    if state.missing_login:
        note += f"\n     (بدون {escape_html(', '.join(state.missing_login))} — همان هشدار بالا)"
    return note


async def run_cookie_watch(
    stop_event: asyncio.Event,
    bot: Bot,
    admin_ids: Iterable[int],
    watcher: CookieJarWatcher,
    pool: asyncpg.Pool | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    """Poll the jar; notify the admins the first time a fresh export waits.

    ``pool`` records the export as a fix, which is what lets ``/trend`` compare the
    failures on either side of it — the only way to tell whether an export helped.
    ``settings`` hands the same export to the fallback engine as well (see
    :func:`cobalt_cookie_note`).
    """
    logger.info("cookie-jar watcher started (every %.0fs)", watcher.interval_s)
    while not stop_event.is_set():
        try:
            # Waiting on the stop event instead of sleeping means shutdown does
            # not have to outlast a whole interval.
            await asyncio.wait_for(stop_event.wait(), timeout=watcher.interval_s)
            break
        except asyncio.TimeoutError:
            pass
        try:
            alert = watcher.inspect()
            if alert is not None:
                text = alert.text
                if alert.kind == "fresh":
                    # Verify before telling: whether this export works is the whole
                    # question, and only a request can answer it. The probe also
                    # takes the export (the copy is refreshed), so the alert's
                    # "the next download picks it up" stays true.
                    text = f"{text}\n\n{await verify_export(watcher.extractor, alert.state)}"
                    # The same export is the fallback engine's login too — and it
                    # needs a restart to see it. The jar is the one the alert is
                    # about, not a second lookup of the same thing.
                    text += cobalt_cookie_note(settings, alert.state.path)
                # The button is what keeps the alert actionable from the chat:
                # it runs the same report /doctor prints.
                delivered = await notify_admins(
                    bot, admin_ids, text, reply_markup=alert_keyboard()
                )
                logger.info("told %s admin(s) about the new cookie jar", delivered)
                if delivered and alert.kind == "fresh" and pool is not None:
                    await telemetry.record_fix(
                        pool,
                        kind="cookie_jar",
                        detail="اکسپورت تازه روی دیسک برداشته شد",
                    )
        except Exception:
            # A watcher must never be the reason the bot dies.
            logger.exception("cookie-jar watch cycle failed")
    logger.info("cookie-jar watcher stopped")
