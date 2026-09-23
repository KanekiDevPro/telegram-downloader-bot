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

import asyncio
import logging
from typing import Any

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core.config import get_settings
from core.i18n import DEFAULT_LANG, t
from core.ui import Screen, edit_quietly
from core.utils import escape_html
from handlers.user import _back_to_menu, support_target
from services import broadcast, cookie_refresh, login_wizard, panel
from services.cobalt import CobaltService
from services.cookie_refresh import RefreshOutcome, render_outcome
from services.cookie_watch import DOCTOR_CALLBACK, REFRESH_CALLBACK
from services.doctor import DEFAULT_PROBE_URL, DoctorReport, fallback_health, run_youtube_doctor
from services.extractor import ExtractorService
from services.oauth import (
    DEVICE_URL,
    FlowRunning,
    OAuthOutcome,
    OAuthService,
    cache_state_line,
    wait_for_code,
)
from services.queue import TaskQueue
from services.telemetry import (
    DIGEST_DAYS,
    TREND_DAYS,
    build_digest,
    build_recent_digest,
    build_trend,
    render_digest,
    render_trend,
)

logger = logging.getLogger(__name__)
router = Router(name="admin")

#: Telegram hard-limits a message to 4096 characters; leave room for the wrappers.
CHUNK_LIMIT = 3800

#: The command menu Telegram draws for *everyone*, as i18n keys (the client shows
#: this list when someone types "/").
USER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "cmd.start"),
    ("download", "cmd.download"),
    ("profile", "cmd.profile"),
    ("premium", "cmd.premium"),
    ("language", "cmd.language"),
    ("help", "cmd.help"),
)

#: ...and the extra ones each admin chat gets. Registered per chat
#: (``BotCommandScopeChat``) instead of globally, because a normal user's "/" menu
#: should not advertise a panel they cannot open — and an admin should not have to
#: remember that these commands exist at all, which is exactly how `/admin` was
#: reported as "missing" while it was working the whole time.
ADMIN_COMMANDS: tuple[tuple[str, str], ...] = (
    ("admin", "cmd.admin"),
    ("doctor", "cmd.doctor"),
    ("blocks", "cmd.blocks"),
    ("trend", "cmd.trend"),
    ("refresh", "cmd.refresh"),
    ("fixlogin", "cmd.fixlogin"),
    ("oauth", "cmd.oauth"),
    ("broadcast", "cmd.broadcast"),
    ("status", "cmd.status"),
)


async def publish_commands(bot: Bot, lang: str = DEFAULT_LANG) -> None:
    """Tell Telegram which commands exist, per scope. Never raises.

    Called once at startup: a command menu is a *courtesy* for the human typing "/",
    and a deployment must not fail to boot because Telegram refused to store it. A
    failed per-admin scope is logged loudly — that one leaves an operator with a
    working but invisible panel.
    """
    settings = get_settings()
    try:
        await bot.set_my_commands(
            [BotCommand(command=name, description=t(key, lang)) for name, key in USER_COMMANDS]
        )
    except Exception:  # noqa: BLE001 — a nice-to-have, never a startup gate
        logger.warning("could not publish the user command list", exc_info=True)
    for admin_id in sorted(settings.admin_ids):
        try:
            await bot.set_my_commands(
                [
                    *(BotCommand(command=name, description=t(key, lang)) for name, key in USER_COMMANDS),
                    *(BotCommand(command=name, description=t(key, lang)) for name, key in ADMIN_COMMANDS),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:  # noqa: BLE001 — an admin who never opened a chat with the bot
            logger.warning(
                "could not publish the admin command list for %s — the commands still work, "
                "they are just not listed in that chat's command menu",
                admin_id,
                exc_info=True,
            )


class AdminStates(StatesGroup):
    """The panel's typed inputs: an announcement, a support contact, a lookup.

    The first two go through a confirmation instead of acting on what arrives: a
    broadcast reaches every user and cannot be taken back, and a mistyped support
    handle is a broken button for everybody until somebody notices. The lookup is
    harmless either way — it only reads.
    """

    broadcast = State()
    support = State()
    user_search = State()


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


async def _say(
    edit: Message | None, bot: Bot, chat_id: int, text: str, **kwargs: Any
) -> None:
    """Tell the admin something went wrong, in whichever message is ours."""
    try:
        if edit is not None:
            await edit.edit_text(text, **kwargs)
        else:
            await bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.debug("could not report the doctor failure", exc_info=True)


@router.message(Command("doctor", "ytdoctor"))
async def cmd_doctor(
    message: Message,
    bot: Bot,
    extractor: ExtractorService,
    cobalt: CobaltService | None = None,
    pool: asyncpg.Pool | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        # Same wording as the payment callbacks: never pretend the command worked.
        await message.answer(t("admin.only", lang))
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


async def _failure_report(
    pool: asyncpg.Pool, cobalt: CobaltService | None, lang: str = DEFAULT_LANG
) -> str:
    """The weekly digest with the fallback's health under it.

    The two belong together: a digest full of login/IP failures means one thing
    when the safety net is serving those links anyway, and something else entirely
    when it is not. So the same section ``/doctor`` shows is appended — from the
    facts that cost nothing (a quarantine in this process, the last real use), not
    from a probe: ``/doctor`` is where an admin spends a request on a live test.
    One implementation, shared by ``/blocks`` and the panel's screen.
    """
    settings = get_settings()
    digest = await build_digest(pool, days=DIGEST_DAYS)
    health = await fallback_health(settings, cobalt, probe=False, pool=pool)
    headline = t("admin.blocks_headline", lang, days=DIGEST_DAYS)
    return f"{render_digest(digest, headline=headline)}\n\n{health.line()}"


@router.message(Command("blocks"))
async def cmd_blocks(
    message: Message,
    pool: asyncpg.Pool,
    cobalt: CobaltService | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    """``/blocks`` — the weekly digest on demand (same text as the panel's)."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
        return
    await message.answer(await _failure_report(pool, cobalt, lang))


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
    message: Message,
    bot: Bot,
    extractor: ExtractorService,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """``/refresh [profile]`` — read the browser profile into the jar, right now."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
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
async def cmd_trend(
    message: Message, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """``/trend`` — failures per day, and whether the last fix changed them."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
        return
    trend = await build_trend(pool, days=TREND_DAYS)
    await message.answer(
        render_trend(trend, headline=t("admin.trend_headline", lang, days=TREND_DAYS))
    )


@router.message(Command("fixlogin"))
async def cmd_fixlogin(
    message: Message, extractor: ExtractorService, lang: str = DEFAULT_LANG
) -> None:
    """``/fixlogin`` — the guided way to a jar that actually signs YouTube in."""
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
        return
    diagnosis = login_wizard.diagnose(extractor)
    await message.answer(
        login_wizard.render_fixlogin(diagnosis, login_wizard.candidates())
    )


@router.callback_query(F.data == REFRESH_CALLBACK)
async def on_alert_refresh(
    cb: CallbackQuery,
    bot: Bot,
    extractor: ExtractorService,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """The cookie-jar alert's "♻️ اکسپورت دوباره" button."""
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        # Same rule as the doctor button: a forwarded alert is not a free export.
        await cb.answer(t("admin.only", lang), show_alert=True)
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
    lang: str = DEFAULT_LANG,
) -> None:
    """The cookie-jar alert's "بررسی همین حالا" button."""
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        # Alerts only go to admins, but a forwarded message must not turn the
        # button into a free doctor for everyone.
        await cb.answer(t("admin.only", lang), show_alert=True)
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
#: The dashboard's sections, as the hub lists them.
_PANEL_SCREENS: frozenset[str] = frozenset(
    {
        "home",
        "stats",
        "users",
        "broadcast",
        "blocks",
        "trend",
        "failures",
        "groups",
        "system",
        "settings",
    }
)
#: Screens an *older* keyboard may still name. They render as their modern
#: section (queue and health live on System now, tools split between System and
#: Settings) — a forwarded panel from last week must not dead-end.
_PANEL_ALIASES: dict[str, str] = {
    "health": "system",
    "queue": "system",
    "tools": "system",
    "support": "settings",
}
#: The two confirmations (they are not screens: they act).
BC_SEND = "bc:send"
BC_CANCEL = "bc:cancel"
BC_START = "bc:start"
SUP_EDIT = "sup:edit"
SUP_CLEAR = "sup:clear"


def _panel_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The dashboard hub: eight sections in pairs, then the way to the user menu.

    Categories, not one giant list — everything an operator can *read*
    (Statistics, Users, Blocks, Trends, Failures) sits with its peers, actions
    and configuration have their own homes (Broadcast, System, Settings), and
    every section is one screen deep from here. The back button leaves the panel
    entirely: an admin is also a user of the same one-message UI.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_stats", lang), callback_data="admin:stats")
    builder.button(text=t("admin.btn_users", lang), callback_data="admin:users")
    builder.button(text=t("admin.btn_broadcast", lang), callback_data="admin:broadcast")
    builder.button(text=t("admin.btn_blocks", lang), callback_data="admin:blocks")
    builder.button(text=t("admin.btn_trend", lang), callback_data="admin:trend")
    builder.button(text=t("admin.btn_failures", lang), callback_data="admin:failures")
    builder.button(text=t("admin.btn_groups", lang), callback_data="admin:groups")
    builder.button(text=t("admin.btn_system", lang), callback_data="admin:system")
    builder.button(text=t("admin.btn_settings", lang), callback_data="admin:settings")
    builder.adjust(2)
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(2)
    return builder.as_markup()


def _system_keyboard(lang: str) -> InlineKeyboardMarkup:
    """System's own actions, then the way back to the hub.

    The first two call the *same* callbacks the cookie-jar alert uses, so an
    operator who taps here and one who taps there get identical behaviour — one
    implementation and one set of tests. The reload re-reads this screen; fixlogin
    and oauth stay commands (they are long guided flows) and are named in the text.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_doctor", lang), callback_data=DOCTOR_CALLBACK)
    builder.button(text=t("admin.btn_refresh", lang), callback_data=REFRESH_CALLBACK)
    builder.button(text=t("admin.btn_reload", lang), callback_data="admin:system")
    builder.adjust(2)
    builder.button(text=t("admin.btn_back", lang), callback_data=PANEL_HOME)
    builder.adjust(2)
    return builder.as_markup()


def _section_keyboard(lang: str, screen: str) -> InlineKeyboardMarkup:
    """A read-only section: re-read it, or leave to its parent — the hub.

    One shape for every report screen, so the buttons never move around between
    them: refresh on the left, back on the right, and "back" is always the
    section's *parent* (the dashboard hub), never "wherever".
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_reload", lang), callback_data=f"admin:{screen}")
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


def _done_keyboard(lang: str) -> InlineKeyboardMarkup:
    """A finished flow's only question is "where to now" — the hub."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_back", lang), callback_data=PANEL_HOME)
    builder.adjust(1)
    return builder.as_markup()


def _users_keyboard(lang: str, *, offset: int, total: int) -> InlineKeyboardMarkup:
    """The Users screen: a lookup, paging while there is more, and the way back.

    The arrows appear only when they have somewhere to go — a disabled-looking
    button that wraps around is how paging becomes a guessing game. Rows are
    computed from what is actually drawn, so the layout stays honest when one
    arrow is missing.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_search", lang), callback_data="usr:search")
    widths = [1]
    nav = 0
    if offset > 0:
        builder.button(
            text=t("admin.btn_prev", lang),
            callback_data=f"usr:page:{max(0, offset - panel.USERS_PAGE_SIZE)}",
        )
        nav += 1
    if offset + panel.USERS_PAGE_SIZE < total:
        builder.button(
            text=t("admin.btn_next", lang),
            callback_data=f"usr:page:{offset + panel.USERS_PAGE_SIZE}",
        )
        nav += 1
    if nav:
        widths.append(nav)  # prev and next share a row; one alone is fine too
    builder.button(text=t("admin.btn_back", lang), callback_data=PANEL_HOME)
    widths.append(1)
    builder.adjust(*widths)
    return builder.as_markup()


def _support_keyboard(lang: str, *, configured: bool) -> InlineKeyboardMarkup:
    """Write the contact, or remove it (shown only when there is one to remove).

    This *is* the Settings screen's keyboard, so its back leaves for the hub —
    the prompt screen that SUP_EDIT opens is the one that comes back here.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.support_set", lang), callback_data=SUP_EDIT)
    if configured:
        builder.button(text=t("admin.support_clear", lang), callback_data=SUP_CLEAR)
    builder.adjust(2)
    builder.button(text=t("admin.btn_back", lang), callback_data=PANEL_HOME)
    builder.adjust(2)
    return builder.as_markup()


async def panel_screen(
    screen: str,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    cobalt: CobaltService | None,
    lang: str = DEFAULT_LANG,
    *,
    offset: int = 0,
) -> Screen:
    """One screen of the panel: its text and its buttons, as a whole page.

    Every screen is built on demand rather than cached: "how deep is the queue" has
    an answer that is worth one query, precisely because it changes. Each returns
    its *own* keyboard — never a shared menu under foreign text — whose one ⬅️
    leads to this screen's parent: the hub for every section. Names an older
    keyboard still uses resolve to their modern section via ``_PANEL_ALIASES``.
    """
    settings = get_settings()
    if screen == "stats":
        return Screen(await panel.stats_text(pool, lang), _section_keyboard(lang, "stats"))
    if screen == "users":
        return await _users_screen(pool, lang, offset=offset)
    if screen == "broadcast":
        return await _broadcast_screen(pool, lang)
    if screen == "trend":
        trend = await build_trend(pool, days=TREND_DAYS)
        headline = t("admin.trend_headline", lang, days=TREND_DAYS)
        return Screen(render_trend(trend, headline=headline), _section_keyboard(lang, "trend"))
    if screen == "blocks":
        return Screen(await _failure_report(pool, cobalt, lang), _section_keyboard(lang, "blocks"))
    if screen == "groups":
        return Screen(await panel.groups_text(pool, lang), _section_keyboard(lang, "groups"))
    if screen == "failures":
        # The short window on purpose: Trends owns the long view, this screen
        # answers "is it failing *right now*" — and what to do about it.
        digest = await build_recent_digest(pool)
        text = render_digest(digest, headline=t("admin.failures_headline", lang))
        return Screen(text, _section_keyboard(lang, "failures"))
    if screen in ("system", "health", "queue", "tools"):
        health = await panel.health_text(pool, queue, settings, cobalt, lang)
        queue_line = await panel.queue_text(queue, settings, lang)
        text = "\n".join((health, "", queue_line, "", panel.tools_text(lang)))
        return Screen(text, _system_keyboard(lang))
    if screen in ("settings", "support"):
        return await _support_screen(pool, lang)
    return Screen(await panel.header(pool, lang), _panel_keyboard(lang))


async def _users_screen(pool: asyncpg.Pool, lang: str, *, offset: int = 0) -> Screen:
    """Totals over everybody, then one page of the newest accounts."""
    text = await panel.users_text(pool, lang, offset=offset)
    total = await database.count_users(pool)
    return Screen(text, _users_keyboard(lang, offset=offset, total=total))


async def _broadcast_screen(pool: asyncpg.Pool, lang: str) -> Screen:
    """What a broadcast will reach, before anyone writes it.

    The count is read here rather than remembered from the last run: an operator
    about to message everybody should see how many that is *now*.
    """
    total = await database.count_users(pool)
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.broadcast_start", lang), callback_data=BC_START)
    builder.button(text=t("admin.btn_back", lang), callback_data=PANEL_HOME)
    builder.adjust(2)
    return Screen(t("admin.broadcast_intro", lang, users=total), builder.as_markup())


async def _support_screen(pool: asyncpg.Pool, lang: str) -> Screen:
    """Where the support button points right now, and how to change it."""
    contact = await database.get_support_contact(pool)
    shown = contact or t("admin.support_none", lang)
    return Screen(
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
    screen = await panel_screen("home", pool, queue, cobalt, lang)
    await message.answer(screen.text, reply_markup=screen.keyboard)


@router.callback_query(F.data == "menu:admin")
async def on_menu_admin(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    cobalt: CobaltService | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    """The panel, one tap from the main menu — for admins only.

    The admin check is the same one `/admin` makes, and it runs here too: the menu
    button is drawn from the *stored* admin list, but a message can be forwarded and
    a keyboard travels with it.
    """
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    screen = await panel_screen("home", pool, queue, cobalt, lang)
    await cb.answer()
    if message is None:
        return
    await _edit(message, screen.text, reply_markup=screen.keyboard)


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
    known = screen if screen in _PANEL_SCREENS or screen in _PANEL_ALIASES else "home"
    page = await panel_screen(known, pool, queue, cobalt, lang)
    # Identical content (a tap on the screen already open) is answered with
    # silence — see core.ui.edit_quietly; a duplicate panel helps nobody.
    await _edit(message, page.text, reply_markup=page.keyboard)


# ---------------------------------------------------------------------------
# The Users section: paging and lookup (reads only)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("usr:page:"))
async def on_users_page(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """A paging arrow: the Users screen again, one page further along."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    raw = (cb.data or "").rsplit(":", 1)[-1]
    try:
        offset = max(0, int(raw))
    except ValueError:
        offset = 0
    await cb.answer()
    screen = await _users_screen(pool, lang, offset=offset)
    await _edit(message, screen.text, reply_markup=screen.keyboard)


@router.callback_query(F.data == "usr:search")
async def on_users_search(
    cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG
) -> None:
    """Ask for the lookup query; the next message from this admin is it."""
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer()
    await state.set_state(AdminStates.user_search)
    await _edit(
        message,
        t("admin.users_search_prompt", lang),
        reply_markup=_back_to_menu(lang, to="admin:users"),
    )


@router.message(AdminStates.user_search)
async def on_users_search_value(
    message: Message, state: FSMContext, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """The lookup itself — answered as a screen, never as raw table output.

    A read only (the section changes nothing), so the query is not even logged:
    who looked somebody up is an audit fact, what they typed is that user's
    business.
    """
    user = message.from_user
    if not get_settings().is_admin(user.id if user else None):
        await state.clear()
        await message.answer(t("admin.only", lang))
        return
    query = (message.text or "").strip()
    await state.clear()
    if not query:
        await message.answer(
            t("admin.users_search_prompt", lang),
            reply_markup=_back_to_menu(lang, to="admin:users"),
        )
        return
    text = await panel.users_search_text(pool, query, lang)
    await message.answer(text, reply_markup=_back_to_menu(lang, to="admin:users"))
    logger.info("user lookup ran for admin %s", user.id if user else "?")


# ---------------------------------------------------------------------------
# The OAuth2 device flow: a YouTube login with no browser and no cookies
# ---------------------------------------------------------------------------


@router.message(Command("oauth"))
async def cmd_oauth(
    message: Message, bot: Bot, oauth: OAuthService | None = None, lang: str = DEFAULT_LANG
) -> None:
    """``/oauth`` — log YouTube in through the Smart-TV device flow.

    One admin command, three honest outcomes: the device code arrives *here* the
    moment the child prints it (the handler does not wait for the flow to finish
    to show it — the code expires in half an hour and the admin is already
    waiting), the refusal arrives when the installed yt-dlp cannot do the flow at
    all (revoked upstream; a reviving plugin re-enables it), and the failure
    arrives with the child's own last words. A second command while one is live
    is answered, not queued.
    """
    settings = get_settings()
    user = message.from_user
    if not settings.is_admin(user.id if user else None):
        await message.answer(t("admin.only", lang))
        return
    if oauth is None:
        await message.answer("❌ سرویس OAuth در دسترس نیست.")
        return

    supported, evidence = await oauth.supported()
    if not supported:
        await message.answer(
            "❌ این yt-dlp امکان لاگین OAuth را ندارد — یوتیوب این مسیر را بسته و پاسخ "
            "سمت yt-dlp این است:\n\n"
            f"<code>{escape_html(evidence[:300])}</code>\n\n"
            "🔒 مسیر درست همین حالا: کوکی لاگین‌شده (<code>/fixlogin</code>). "
            "اگر پلاگینی که OAuth را برمی‌گرداند نصب شود، همین دستور خودکار کار می‌کند.",
        )
        return

    try:
        sink: asyncio.Queue[str] = asyncio.Queue()
        flow = await oauth.start(DEFAULT_PROBE_URL, code_sink=sink)
    except FlowRunning:
        await message.answer(
            "⏳ یک لاگین OAuth همین حالا در جریان است — کدش را وارد کنید یا صبر کنید "
            "تا تمام شود؛ همزمان دو لاگین ممکن نیست."
        )
        return

    status = await message.answer(
        "📺 در حال شروع لاگین Smart TV… (کد تا چند دقیقه می‌رسد)"
    )

    # The child prints the code to stderr; the sink is how it crosses into this
    # handler *while the flow is still running*. A silent child (a refusal the
    # probe could not have seen, a network error) ends the wait with no code —
    # and then the flow's own outcome explains why.
    code = await wait_for_code(sink, timeout_s=30.0)
    if code is None:
        await flow.cancel()
        await oauth.finish(flow)
        outcome = OAuthOutcome("failed", detail="no device code was ever printed")
        await _say(status, bot, message.chat.id, _oauth_failure_text(outcome))
        return

    keyboard = InlineKeyboardBuilder().button(text="🔓 باز کردن صفحهٔ ورود", url=DEVICE_URL).as_markup()
    await _say(
        status,
        bot,
        message.chat.id,
        "📺 <b>لاگین Smart TV یوتیوب</b>\n\n"
        f"1. روی دکمهٔ زیر بزنید ({DEVICE_URL})\n"
        f"2. این کد را وارد کنید: <code>{escape_html(code)}</code>\n"
        "3. دسترسی را تأیید کنید — بقیه‌اش خودکار است.\n\n"
        "⏳ تا وقتی این پنجره باز است، منتظر تأیید شما می‌مانم…",
        reply_markup=keyboard,
    )

    outcome = await flow.run(DEFAULT_PROBE_URL)
    await oauth.finish(flow)
    logger.info("OAuth device flow finished: %s (code=%s)", outcome.status, outcome.code)
    if outcome.status == "success":
        await _say(
            None,
            bot,
            message.chat.id,
            "✅ <b>OAuth2 لاگین شد!</b> توکن TV در کش yt-dlp ذخیره شد — یوتیوب دیگر "
            "بدون کوکی و بدون IP تمیز هم جواب می‌دهد.\n\n"
            f"🔑 کد استفاده‌شده: <code>{escape_html(outcome.code or '—')}</code>\n"
            f"🗂 {escape_html(cache_state_line(settings.ytdlp_cache_dir))}"
            + _oauth_off_note(settings),
        )
    else:
        await _say(None, bot, message.chat.id, _oauth_failure_text(outcome))


def _oauth_off_note(settings: Any) -> str:
    """The one nudge a successful login still needs: turn the switch on."""
    if settings.ytdlp_use_oauth2:
        return ""
    return (
        "\n\n⚠️ در <code>.env</code> مقدار <code>YTDLP_USE_OAUTH2=1</code> نیست — "
        "تا وقتی که روشنش نکنید، دانلودها از این لاگین استفاده نمی‌کنند."
    )


def _oauth_failure_text(outcome: OAuthOutcome) -> str:
    """What a failed flow says, in the words of *why* it failed."""
    if outcome.status == "unsupported":
        return (
            "❌ لاگین OAuth از سمت yt-dlp رد شد — یوتیوب این مسیر را بسته است:\n\n"
            f"<code>{escape_html(outcome.detail[:300])}</code>"
        )
    if outcome.status == "expired":
        return (
            "⌛️ کد وارد نشد و مهلتش تمام شد — دوباره <code>/oauth</code> بزنید و کد را "
            "در همان نیم ساعت وارد کنید."
        )
    detail = f"\n\n<code>{escape_html(outcome.detail[:300])}</code>" if outcome.detail else ""
    return f"❌ لاگین OAuth تمام نشد ({outcome.status}).{detail}"


# ---------------------------------------------------------------------------
# The panel's two typed inputs: a broadcast, and the support contact
# ---------------------------------------------------------------------------


async def _edit(message: Message, text: str, **kwargs: Any) -> None:
    """Rewrite a panel message, tolerating anything Telegram will not touch.

    The panel's contract, spelled out in ``core.ui.edit_quietly`` (this is it,
    under the name every handler here already knows): the *action* behind a
    button has already happened, so a failed status rewrite is nobody's error.
    """
    await edit_quietly(message, text, **kwargs)


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
    # The prompt is a screen too: it carries its own [Cancel], not the keyboard
    # of the screen it replaced — and tapping it must never re-arm the flow.
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.broadcast_cancel", lang), callback_data=BC_CANCEL)
    builder.adjust(1)
    await _edit(
        message,
        t("admin.broadcast_prompt", lang),
        reply_markup=builder.as_markup(),
    )


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
        await _edit(
            message,
            t("admin.broadcast_cancelled", lang),
            reply_markup=_done_keyboard(lang),
        )


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

    empty = InlineKeyboardMarkup(inline_keyboard=[])

    async def progress(sent: int, total: int) -> None:
        # An *empty* keyboard, on purpose: the confirm buttons must be gone the
        # moment sending starts, or a second tap could look like a second offer.
        await _edit(
            message,
            t("admin.broadcast_sending", lang, sent=sent, total=total),
            reply_markup=empty,
        )

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
        reply_markup=_done_keyboard(lang),
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
    await _edit(
        message,
        t("admin.support_prompt", lang),
        reply_markup=_back_to_menu(lang, to="admin:settings"),
    )


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
