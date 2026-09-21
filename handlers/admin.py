"""Admin-only diagnostics.

``/doctor`` answers the question the logs only hint at: *why* is YouTube
refusing? It runs the full chain (cookie login, JavaScript runtime, PO-token
provider, ffmpeg, optional proxy) plus a live metadata probe and replies with one
verdict and the one fix worth trying next.

The rest answer the questions that follow it: ``/refresh`` re-exports the jar from
a browser profile on demand, ``/fixlogin`` walks through getting a *real* YouTube
login into it, ``/trend`` says whether the last fix actually reduced the failures,
and ``/blocks`` prints the weekly digest — with the fallback engine's health under
it, because "how many failed" reads very differently once the safety net is down.
The same first two are one tap away from the cookie-jar alert, so an alert an admin
receives is not a dead end.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core.config import get_settings
from core.i18n import DEFAULT_LANG, t
from core.utils import escape_html
from handlers.user import support_target
from services import broadcast, cookie_refresh, login_wizard, panel
from services.cobalt import CobaltService
from services.cookie_refresh import RefreshOutcome, render_outcome
from services.cookie_watch import DOCTOR_CALLBACK, REFRESH_CALLBACK
from services.doctor import DoctorReport, fallback_health, run_youtube_doctor
from services.extractor import ExtractorService
from services.queue import TaskQueue
from services.telemetry import (
    DIGEST_DAYS,
    TREND_DAYS,
    build_digest,
    build_trend,
    render_digest,
    render_trend,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")

#: Telegram hard-limits a message to 4096 characters; leave room for the wrappers.
CHUNK_LIMIT = 3800


class AdminStates(StatesGroup):
    """The panel's two typed inputs: an announcement, and the support contact.

    Both go through a confirmation instead of acting on what arrives: a broadcast
    reaches every user and cannot be taken back, and a mistyped support handle is a
    broken button for everybody until somebody notices. One extra tap each.
    """

    broadcast = State()
    support = State()


def _chunks(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split by lines so a long report can be sent as several messages."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


def report_parts(report: DoctorReport) -> list[str]:
    """The report as Telegram messages: escaped, pre-formatted, chunked."""
    return [f"<pre>{escape_html(chunk)}</pre>" for chunk in _chunks(report.render())]


async def deliver_report(
    bot: Bot, chat_id: int, parts: list[str], *, edit: Message | None = None
) -> None:
    """Put the report where the admin asked for it.

    With ``edit`` the first part replaces the message the admin is already
    looking at (the alert's placeholder or the alert itself); the rest follow as
    new messages, since one Telegram message cannot grow past its limit.
    """
    if edit is not None:
        await edit.edit_text(parts[0])
    else:
        await bot.send_message(chat_id, parts[0])
    for part in parts[1:]:
        try:
            await bot.send_message(chat_id, part)
        except Exception:
            logger.exception("could not send doctor report part")
            break


async def run_doctor_into(
    bot: Bot,
    chat_id: int,
    extractor: ExtractorService,
    *,
    cobalt: CobaltService | None = None,
    pool: asyncpg.Pool | None = None,
    edit: Message | None = None,
) -> DoctorReport | None:
    """Run the doctor, render it, and report where it went; ``None`` on failure.

    ``cobalt``/``pool`` are forwarded so the report describes the *running* fallback
    engine (and remembers its verdict) instead of a fresh opinion about the config.
    """
    settings = get_settings()
    try:
        report = await run_youtube_doctor(settings, extractor, cobalt=cobalt, pool=pool)
    except Exception:
        logger.exception("youtube doctor failed")
        await _say(edit, bot, chat_id, "❌ بررسی شکست خورد؛ لاگ سرور را ببینید.")
        return None
    try:
        await deliver_report(bot, chat_id, report_parts(report), edit=edit)
    except Exception:
        logger.exception("could not render doctor report")
        await _say(edit, bot, chat_id, "❌ نمایش گزارش ممکن نشد؛ خروجی را در لاگ ببینید.")
        return None
    return report


async def _say(edit: Message | None, bot: Bot, chat_id: int, text: str) -> None:
    """Tell the admin something went wrong, in whichever message is ours."""
    try:
        if edit is not None:
            await edit.edit_text(text)
        else:
            await bot.send_message(chat_id, text)
    except Exception:
        logger.debug("could not report the doctor failure", exc_info=True)


@router.message(Command("doctor", "ytdoctor"))
async def cmd_doctor(
    message: Message,
    bot: Bot,
    extractor: ExtractorService,
    cobalt: CobaltService | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        # Same wording as the payment callbacks: never pretend the command worked.
        await message.answer("⛔️ فقط ادمین می‌تونه.")
        return

    status = await message.answer("🔎 در حال بررسی مسیر دانلود یوتیوب…")
    report = await run_doctor_into(
        bot, message.chat.id, extractor, cobalt=cobalt, pool=pool, edit=status
    )
    if report is not None:
        logger.info(
            "doctor report requested by admin %s: healthy=%s",
            user.id if user else "?",
            report.healthy,
        )


@router.message(Command("blocks"))
async def cmd_blocks(
    message: Message, pool: asyncpg.Pool, cobalt: CobaltService | None = None
) -> None:
    """The weekly digest on demand, with the fallback's health under it.

    The two belong together: a digest full of login/IP failures means one thing
    when the safety net is serving those links anyway, and something else entirely
    when it is not. So the same section ``/doctor`` shows is appended — from the
    facts that cost nothing (a quarantine in this process, the last real use), not
    from a probe: ``/doctor`` is where an admin spends a request on a live test.
    """
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer("⛔️ فقط ادمین می‌تونه.")
        return
    digest = await build_digest(pool, days=DIGEST_DAYS)
    health = await fallback_health(settings, cobalt, probe=False, pool=pool)
    report = (
        f"{render_digest(digest, headline=f'گزارش {DIGEST_DAYS} روزهٔ شکست‌ها')}"
        f"\n\n{health.line()}"
    )
    await message.answer(report)


def refresh_usage() -> str:
    """What to type when no profile is named (off, or a manual try)."""
    return "\n".join(
        [
            "♻️ برای اکسپورت دستی یک پروفایل لازم است تا کوکی از آن خوانده شود:",
            "",
            "• <code>/refresh edge:Default</code> — یا chrome، firefox، brave، …",
            "• یا <code>COOKIE_AUTO_EXPORT=edge:Default</code> در <code>.env</code> "
            "تا بعد از هر بلاک لاگین خودش هم انجام شود.",
            "",
            "📍 فقط روی ماشینی معنا دارد که مرورگر لاگین‌شده دارد؛ داخل کانتینر "
            "مرورگری نیست و پیام «پروفایل پیدا نشد» می‌گیرید — آنجا اکسپورت روی "
            "هاست انجام می‌شود.",
        ]
    )


async def run_refresh_into(
    bot: Bot,
    chat_id: int,
    extractor: ExtractorService,
    pool: asyncpg.Pool,
    *,
    spec: str,
    edit: Message | None = None,
) -> RefreshOutcome | None:
    """Run an on-demand refresh and put the outcome where the admin asked for it."""
    settings = get_settings()
    outcome = await cookie_refresh.auto_refresh_jar(
        settings, extractor, bot, settings.admin_ids, force=True, spec=spec, pool=pool
    )
    if outcome is None:
        # The only reason left, once force is on and a profile is named: another
        # export is already running (the cooldown and the profile are handled).
        await _say(
            edit, bot, chat_id, "♻️ یک اکسپورت همین حالا در جریان است — کمی بعد دوباره بزنید."
        )
        return None
    await _say(edit, bot, chat_id, render_outcome(outcome) + "\n\n📣 همین برای ادمین‌ها هم رفت.")
    return outcome


@router.message(Command("refresh"))
async def cmd_refresh(
    message: Message, bot: Bot, extractor: ExtractorService, pool: asyncpg.Pool
) -> None:
    """``/refresh [profile]`` — read the browser profile into the jar, right now."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer("⛔️ فقط ادمین می‌تونه.")
        return

    argument = (message.text or "").partition(" ")[2].strip()
    spec = argument or settings.cookie_auto_export.strip()
    if not spec:
        await message.answer(refresh_usage())
        return

    status = await message.answer("♻️ در حال خواندن کوکی از پروفایل…")
    outcome = await run_refresh_into(
        bot, message.chat.id, extractor, pool, spec=spec, edit=status
    )
    logger.info(
        "on-demand cookie refresh by %s: %s",
        user.id if user else "?",
        getattr(outcome, "kind", None),
    )


@router.message(Command("trend"))
async def cmd_trend(message: Message, pool: asyncpg.Pool) -> None:
    """``/trend`` — failures per day, and whether the last fix changed them."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer("⛔️ فقط ادمین می‌تونه.")
        return
    trend = await build_trend(pool, days=TREND_DAYS)
    await message.answer(render_trend(trend, headline=f"روند {TREND_DAYS} روزهٔ شکست‌ها"))


@router.message(Command("fixlogin"))
async def cmd_fixlogin(message: Message, extractor: ExtractorService) -> None:
    """``/fixlogin`` — the guided way to a jar that actually signs YouTube in."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer("⛔️ فقط ادمین می‌تونه.")
        return
    diagnosis = login_wizard.diagnose(extractor)
    await message.answer(
        login_wizard.render_fixlogin(diagnosis, login_wizard.candidates())
    )


@router.callback_query(F.data == REFRESH_CALLBACK)
async def on_alert_refresh(
    cb: CallbackQuery, bot: Bot, extractor: ExtractorService, pool: asyncpg.Pool
) -> None:
    """The cookie-jar alert's "♻️ اکسپورت دوباره" button."""
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        # Same rule as the doctor button: a forwarded alert is not a free export.
        await cb.answer("⛔️ فقط ادمین می‌تونه.", show_alert=True)
        return

    message = cb.message if isinstance(cb.message, Message) else None
    spec = settings.cookie_auto_export.strip()
    if message is None:
        await cb.answer("این پیام دیگه در دسترس نیست؛ /refresh رو بزن.", show_alert=True)
        return
    if not spec:
        await cb.answer("پروفایلی تنظیم نشده (COOKIE_AUTO_EXPORT).", show_alert=True)
        await message.answer(refresh_usage())
        return

    await cb.answer("♻️ در حال اکسپورت…")
    try:
        # The export plus probe can take a while; keep the alert from looking dead.
        await message.edit_text("♻️ در حال خواندن کوکی از پروفایل…")
    except Exception:
        logger.debug("could not show the refresh placeholder", exc_info=True)
    outcome = await run_refresh_into(
        bot, message.chat.id, extractor, pool, spec=spec, edit=message
    )
    logger.info(
        "cookie export ran from the alert for admin %s: %s",
        cb.from_user.id,
        getattr(outcome, "kind", None),
    )


@router.callback_query(F.data == DOCTOR_CALLBACK)
async def on_alert_check(
    cb: CallbackQuery,
    bot: Bot,
    extractor: ExtractorService,
    cobalt: CobaltService | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    """The cookie-jar alert's "بررسی همین حالا" button."""
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        # Alerts only go to admins, but a forwarded message must not turn the
        # button into a free doctor for everyone.
        await cb.answer("⛔️ فقط ادمین می‌تونه.", show_alert=True)
        return

    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer("این پیام دیگه در دسترس نیست؛ /doctor رو بزن.", show_alert=True)
        return

    await cb.answer("🔎 در حال بررسی…")
    try:
        # Keeps the alert itself out of the way while the probe runs (up to a
        # minute on a slow network) instead of looking like nothing happened.
        await message.edit_text("🔎 در حال بررسی مسیر دانلود یوتیوب…")
    except Exception:
        logger.debug("could not show the doctor placeholder", exc_info=True)

    report = await run_doctor_into(
        bot, message.chat.id, extractor, cobalt=cobalt, pool=pool, edit=message
    )
    if report is not None:
        logger.info(
            "doctor report ran from the cookie-jar alert for admin %s: healthy=%s",
            cb.from_user.id,
            report.healthy,
        )


# ---------------------------------------------------------------------------
# The panel: /admin, and the four screens behind it
# ---------------------------------------------------------------------------

#: The panel's own callback namespace (``admin:<screen>``).
PANEL_HOME = "admin:home"
_PANEL_SCREENS: frozenset[str] = frozenset(
    {"home", "stats", "health", "queue", "tools", "broadcast", "support"}
)
#: The two confirmations (they are not screens: they act).
BC_SEND = "bc:send"
BC_CANCEL = "bc:cancel"
BC_START = "bc:start"
SUP_EDIT = "sup:edit"
SUP_CLEAR = "sup:clear"


def _panel_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The four reading screens, in two rows, plus the way back to the user menu."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_stats", lang), callback_data="admin:stats")
    builder.button(text=t("admin.btn_health", lang), callback_data="admin:health")
    builder.button(text=t("admin.btn_queue", lang), callback_data="admin:queue")
    builder.button(text=t("admin.btn_tools", lang), callback_data="admin:tools")
    builder.adjust(2)
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(2)
    return builder.as_markup()


def _tools_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Everything an operator *does* from here.

    The first two call the *same* callbacks the cookie-jar alert uses, so an
    operator who taps here and one who taps there get identical behaviour — one
    implementation and one set of tests. The last two are the panel's newer
    actions: an announcement, and the support button every user sees.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_doctor", lang), callback_data=DOCTOR_CALLBACK)
    builder.button(text=t("admin.btn_refresh", lang), callback_data=REFRESH_CALLBACK)
    builder.button(text=t("admin.btn_broadcast", lang), callback_data="admin:broadcast")
    builder.button(text=t("admin.btn_support", lang), callback_data="admin:support")
    builder.adjust(2)
    builder.button(text=t("admin.btn_back", lang), callback_data=PANEL_HOME)
    builder.adjust(2)
    return builder.as_markup()


def _broadcast_keyboard(lang: str) -> InlineKeyboardMarkup:
    """An announcement waiting for a decision: send it, or walk away."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.broadcast_go", lang), callback_data=BC_SEND)
    builder.button(text=t("admin.broadcast_cancel", lang), callback_data=BC_CANCEL)
    builder.adjust(2)
    return builder.as_markup()


def _support_keyboard(lang: str, *, configured: bool) -> InlineKeyboardMarkup:
    """Write the contact, or remove it (shown only when there is one to remove)."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.support_set", lang), callback_data=SUP_EDIT)
    if configured:
        builder.button(text=t("admin.support_clear", lang), callback_data=SUP_CLEAR)
    builder.adjust(2)
    builder.button(text=t("admin.btn_back", lang), callback_data="admin:tools")
    builder.adjust(2)
    return builder.as_markup()


async def panel_screen(
    screen: str,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    cobalt: CobaltService | None,
    lang: str = DEFAULT_LANG,
) -> tuple[str, InlineKeyboardMarkup]:
    """One screen of the panel: its text and its buttons.

    Every screen is built on demand rather than cached: "how deep is the queue" has
    an answer that is worth one query, precisely because it changes.
    """
    settings = get_settings()
    if screen == "stats":
        return await panel.stats_text(pool, lang), _panel_keyboard(lang)
    if screen == "health":
        text = await panel.health_text(pool, queue, settings, cobalt, lang)
        return text, _panel_keyboard(lang)
    if screen == "queue":
        return await panel.queue_text(queue, settings, lang), _panel_keyboard(lang)
    if screen == "tools":
        return panel.tools_text(lang), _tools_keyboard(lang)
    if screen == "broadcast":
        return await _broadcast_screen(pool, lang)
    if screen == "support":
        return await _support_screen(pool, lang)
    return await panel.header(pool, lang), _panel_keyboard(lang)


async def _broadcast_screen(pool: asyncpg.Pool, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """What a broadcast will reach, before anyone writes it.

    The count is read here rather than remembered from the last run: an operator
    about to message everybody should see how many that is *now*.
    """
    total = await database.count_users(pool)
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.broadcast_start", lang), callback_data=BC_START)
    builder.button(text=t("admin.btn_back", lang), callback_data="admin:tools")
    builder.adjust(2)
    return t("admin.broadcast_intro", lang, users=total), builder.as_markup()


async def _support_screen(pool: asyncpg.Pool, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    """Where the support button points right now, and how to change it."""
    contact = await database.get_support_contact(pool)
    shown = contact or t("admin.support_none", lang)
    return (
        t("admin.support_intro", lang, contact=escape_html(shown)),
        _support_keyboard(lang, configured=bool(contact)),
    )


@router.message(Command("admin"))
async def cmd_admin(
    message: Message,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    cobalt: CobaltService | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    """``/admin`` — the panel, for admins only."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
        return
    text, keyboard = await panel_screen("home", pool, queue, cobalt, lang)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("admin:"))
async def on_panel_button(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    cobalt: CobaltService | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    """A panel button: rewrite the same message with the screen it asked for.

    The admin check is repeated here rather than trusted from the command that drew
    the keyboard: a forwarded message carries the buttons with it, and a panel anyone
    can open is a panel whose numbers are not private.
    """
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    screen = (cb.data or "").split(":", 1)[1]
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer()
    text, keyboard = await panel_screen(
        screen if screen in _PANEL_SCREENS else "home", pool, queue, cobalt, lang
    )
    try:
        await message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        # Identical content (a tap on the screen already open) is not an error worth
        # a new message; a message that cannot be edited at all is.
        logger.debug("panel edit skipped (%s)", screen, exc_info=True)


# ---------------------------------------------------------------------------
# The panel's two typed inputs: a broadcast, and the support contact
# ---------------------------------------------------------------------------


async def _edit(message: Message, text: str, **kwargs: Any) -> None:
    """Replace a panel message in place, tolerating one Telegram will not touch.

    A panel message that cannot be edited (too old, or already carrying exactly
    this text) is not an error worth interrupting an operator with: the *action*
    behind the button is what matters, and it has already happened.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        logger.debug("could not edit the panel message", exc_info=True)


@router.message(Command("broadcast"))
async def cmd_broadcast(
    message: Message, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """``/broadcast`` — the same screen the panel button opens."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
        return
    text, keyboard = await _broadcast_screen(pool, lang)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == BC_START)
async def on_broadcast_start(
    cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG
) -> None:
    """Ask for the text; the next message from this admin is the announcement."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer()
    await state.set_state(AdminStates.broadcast)
    await _edit(message, t("admin.broadcast_prompt", lang))


@router.message(AdminStates.broadcast)
async def on_broadcast_draft(
    message: Message, state: FSMContext, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """The announcement itself — shown back as a *copy*, then confirmed.

    A copy rather than the text pasted into a template: the message that appears is
    byte-for-byte what the users would receive, formatting included, and it cannot
    be broken by an announcement whose markup happens to be unbalanced.
    """
    user = message.from_user
    if not get_settings().is_admin(user.id if user else None):
        await state.clear()
        await message.answer(t("admin.only", lang))
        return
    announcement = (message.html_text or message.text or "").strip()
    if not announcement:
        await message.answer(t("admin.broadcast_empty", lang))
        return
    total = await database.count_users(pool)
    if total == 0:
        await state.clear()
        await message.answer(t("admin.broadcast_no_users", lang))
        return
    await state.update_data(announcement=announcement)
    try:
        await message.copy_to(message.chat.id, reply_markup=_broadcast_keyboard(lang))
    except TelegramBadRequest:
        # The copy is a courtesy; an announcement Telegram will not re-send is one
        # worth refusing anyway, so the question is asked without the preview.
        logger.info("could not show the broadcast preview as a copy", exc_info=True)
        await message.answer(
            t("admin.broadcast_preview", lang, users=total),
            reply_markup=_broadcast_keyboard(lang),
        )
        return
    await message.answer(t("admin.broadcast_preview", lang, users=total))


@router.callback_query(F.data == BC_CANCEL)
async def on_broadcast_cancel(cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG) -> None:
    """Nothing was sent, and the panel says so."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    await state.clear()
    message = cb.message if isinstance(cb.message, Message) else None
    await cb.answer()
    if message is not None:
        await _edit(message, t("admin.broadcast_cancelled", lang))


@router.callback_query(F.data == BC_SEND)
async def on_broadcast_send(
    cb: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """Run the broadcast, with the progress in the message being watched."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    announcement = str((await state.get_data()).get("announcement") or "")
    await state.clear()
    if not announcement:
        # The draft is gone (a restart emptied it, or the panel is old).
        await cb.answer(t("admin.broadcast_cancelled", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer(t("admin.broadcast_sending", lang, sent=0, total=0))

    async def progress(sent: int, total: int) -> None:
        await _edit(message, t("admin.broadcast_sending", lang, sent=sent, total=total))

    try:
        report = await broadcast.deliver(bot, pool, announcement, on_progress=progress)
    except Exception:
        logger.exception("broadcast failed")
        await _edit(message, t("admin.broadcast_failed", lang))
        return
    await _edit(
        message,
        t(
            "admin.broadcast_done",
            lang,
            total=report.total,
            sent=report.sent,
            blocked=report.blocked,
            failed=report.failed,
        ),
    )
    logger.info(
        "broadcast sent by admin %s: %s/%s (%s blocked, %s failed)",
        cb.from_user.id,
        report.sent,
        report.total,
        report.blocked,
        report.failed,
    )


@router.callback_query(F.data == SUP_EDIT)
async def on_support_edit(
    cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG
) -> None:
    """Ask for the new contact (a URL, or an ``@username``)."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer()
    await state.set_state(AdminStates.support)
    await _edit(message, t("admin.support_prompt", lang))


@router.message(AdminStates.support)
async def on_support_value(
    message: Message, state: FSMContext, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """Store the contact — which is what makes the user menu's button appear."""
    user = message.from_user
    if not get_settings().is_admin(user.id if user else None):
        await state.clear()
        await message.answer(t("admin.only", lang))
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer(t("admin.support_prompt", lang))
        return
    await database.set_support_contact(pool, value)
    await state.clear()
    target = support_target(value)
    await message.answer(
        t(
            "admin.support_saved",
            lang,
            contact=escape_html(value),
            kind=t("admin.support_linked" if target else "admin.support_plain", lang),
        ),
        reply_markup=_support_keyboard(lang, configured=True),
    )
    logger.info("support contact set by admin %s: %s", user.id if user else "?", value)


@router.callback_query(F.data == SUP_CLEAR)
async def on_support_clear(
    cb: CallbackQuery, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """Remove the button from every user's menu."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    await cb.answer(t("admin.support_cleared", lang))
    await database.set_support_contact(pool, "")
    if message is None:
        return
    text, keyboard = await _support_screen(pool, lang)
    await _edit(message, text, reply_markup=keyboard)
