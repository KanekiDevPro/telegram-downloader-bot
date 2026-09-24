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
import json
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
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core import texts as text_store
from core.catalog import MESSAGES
from core.config import get_settings
from core.i18n import DEFAULT_LANG, LANGS, t
from core.ui import Screen, edit_quietly
from core.utils import escape_html
from handlers.user import _back_to_menu, support_target
from services import backup as backup_service
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
    #: One user-facing text being rewritten (which key and language travels in
    #: the FSM data — the next message from this admin is the new value).
    text_edit = State()
    #: Waiting for the backup file to restore. The confirm step acts on a
    #: server-side pending restore behind a short nonce — the FSM never holds
    #: the payload (see services/backup.py).
    restore = State()


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
#: The screens the panel serves: the category submenus and the sections inside
#: them. One level deep from the hub, all of them.
_PANEL_SCREENS: frozenset[str] = frozenset(
    {
        "home",
        "stats",
        "users",
        "broadcast",
        "blocks",
        "groups",
        "system",
        "settings",
        "texts",
        "sources",
        # …and the six category submenus themselves.
        "cat_users",
        "cat_downloads",
        "cat_sources",
        "cat_messages",
        "cat_system",
        "cat_diagnostics",
    }
)
#: Screens an *older* keyboard may still name. They render as their modern
#: section — a forwarded panel from last week must not dead-end. The retired
#: "trend"/"failures" screens live on as the diagnostics digest.
_PANEL_ALIASES: dict[str, str] = {
    "health": "system",
    "queue": "system",
    "tools": "system",
    "support": "settings",
    "trend": "blocks",
    "failures": "blocks",
}
#: Every screen's *parent*: Back is the parent, never "wherever" — and a
#: category's own screens come back to it, one level up, always.
_SCREEN_PARENT: dict[str, str] = {
    "stats": "cat_downloads",
    "users": "cat_users",
    "groups": "cat_users",
    "broadcast": "cat_messages",
    "texts": "cat_messages",
    "blocks": "cat_diagnostics",
    "system": "cat_system",
    "settings": "cat_system",
    "sources": "cat_sources",
}
#: The hub's six categories, in hub order: id → its label key.
_CATEGORY_LABELS: dict[str, str] = {
    "cat_users": "admin.cat_users",
    "cat_downloads": "admin.cat_downloads",
    "cat_sources": "admin.cat_sources",
    "cat_messages": "admin.cat_messages",
    "cat_system": "admin.cat_system",
    "cat_diagnostics": "admin.cat_diagnostics",
}
#: Backup & restore — the System submenu's owner-only pair. Short payloads on
#: purpose: Telegram caps callback data at 64 bytes.
BK_BACKUP = "bk:backup"
BK_RESTORE = "bk:restore"
#: The preview's two answers: apply the file that was shown, or walk away. The
#: confirmation carries ONLY a short random nonce — the restore itself lives
#: server-side, bound to the owner and expiring (see services/backup.py).
BK_GO = "bk:go:"
BK_CANCEL = "bk:cancel"

#: …and what each submenu lists: ``(button label key, destination)``. The two
#: extractor actions live under Sources (they are the same callbacks the
#: cookie-jar alert uses — one implementation, one set of tests).
_CATEGORY_ITEMS: dict[str, tuple[tuple[str, str], ...]] = {
    "cat_users": (
        ("admin.btn_users", "admin:users"),
        ("admin.btn_groups", "admin:groups"),
    ),
    "cat_downloads": (("admin.btn_stats", "admin:stats"),),
    "cat_sources": (
        ("admin.btn_doctor", DOCTOR_CALLBACK),
        ("admin.btn_refresh", REFRESH_CALLBACK),
        ("admin.btn_sources", "admin:sources"),
    ),
    "cat_messages": (
        ("admin.btn_texts", "admin:texts"),
        ("admin.btn_broadcast", "admin:broadcast"),
    ),
    "cat_system": (
        ("admin.btn_system", "admin:system"),
        ("admin.btn_settings", "admin:settings"),
        ("admin.btn_backup", BK_BACKUP),
        ("admin.btn_restore", BK_RESTORE),
    ),
    "cat_diagnostics": (("admin.btn_blocks", "admin:blocks"),),
}
#: The texts editor's own callback namespace: ``txt:<action>:…``. Actions and
#: payloads stay short on purpose — Telegram caps callback data at 64 bytes and
#: the longest catalogue key must fit behind the prefix.
TXT_PREFIX = "txt:"
#: How many keys fit on one page of a category's key listing.
TEXTS_PAGE_SIZE = 6
#: The two confirmations (they are not screens: they act).
BC_SEND = "bc:send"
BC_CANCEL = "bc:cancel"
BC_START = "bc:start"
SUP_EDIT = "sup:edit"
SUP_CLEAR = "sup:clear"


def _panel_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The dashboard hub: six categories in pairs, then the way to the user menu.

    Categories, not one giant list — every action lives one screen deep inside
    its category (users with groups, the editable texts with the broadcast, the
    failure views under diagnostics), so the hub itself stays a map anyone can
    read on a phone. The back button leaves the panel entirely: an admin is also
    a user of the same one-message UI.
    """
    builder = InlineKeyboardBuilder()
    for category in _CATEGORY_LABELS:
        builder.button(
            text=t(_CATEGORY_LABELS[category], lang), callback_data=f"admin:{category}"
        )
    builder.adjust(2)
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(2)
    return builder.as_markup()


def _leave(builder: InlineKeyboardBuilder, lang: str, *, to: str) -> None:
    """Every submenu's way out, in one place: Back is the parent, Home is Home.

    Back walks *up* (a screen to its category, a category to the hub); Home
    jumps straight to the user menu every admin also has. Both on every submenu
    — a screen that can only go back is a screen someone gets lost in.
    """
    builder.button(text=t("menu.back", lang), callback_data=to)
    builder.button(text=t("admin.btn_home", lang), callback_data="menu:home")
    builder.adjust(2)


def _category_keyboard(lang: str, category: str) -> InlineKeyboardMarkup:
    """One category's items, then Back (to the hub) and Home."""
    builder = InlineKeyboardBuilder()
    for label_key, destination in _CATEGORY_ITEMS[category]:
        if destination in _OWNER_ACTIONS and not get_settings().owner_id:
            # No OWNER_ID configured: the feature is off entirely — the button
            # would only ever answer "owner only" to every admin. Hidden, not
            # offered (and the handlers refuse anyway).
            continue
        builder.button(text=t(label_key, lang), callback_data=destination)
    builder.adjust(2)
    _leave(builder, lang, to=PANEL_HOME)
    return builder.as_markup()


def _system_keyboard(lang: str) -> InlineKeyboardMarkup:
    """System's own screen: re-read it, back to its category, or home.

    The extractor actions (doctor, cookie re-export) live under *Sources* now —
    one home per action — and fixlogin/oauth stay commands (they are long guided
    flows) and are named in the System screen's text.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_reload", lang), callback_data="admin:system")
    _leave(builder, lang, to=f"admin:{_SCREEN_PARENT['system']}")
    return builder.as_markup()


def _section_keyboard(lang: str, screen: str) -> InlineKeyboardMarkup:
    """A read-only section: re-read it, back to its category, or home.

    One shape for every report screen, so the buttons never move around between
    them: refresh first, then the way out — and "back" is always the section's
    *parent* (its category submenu), never "wherever".
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.btn_reload", lang), callback_data=f"admin:{screen}")
    _leave(builder, lang, to=f"admin:{_SCREEN_PARENT.get(screen, 'home')}")
    return builder.as_markup()


def _broadcast_keyboard(lang: str) -> InlineKeyboardMarkup:
    """An announcement waiting for a decision: send it, or walk away."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.broadcast_go", lang), callback_data=BC_SEND)
    builder.button(text=t("admin.broadcast_cancel", lang), callback_data=BC_CANCEL)
    builder.adjust(2)
    return builder.as_markup()


def _done_keyboard(lang: str) -> InlineKeyboardMarkup:
    """A finished flow's only question is "where to now" — the hub, or Home."""
    builder = InlineKeyboardBuilder()
    _leave(builder, lang, to=PANEL_HOME)
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
    parent = _SCREEN_PARENT.get("users", "home")
    builder.button(text=t("menu.back", lang), callback_data=f"admin:{parent}")
    builder.button(text=t("admin.btn_home", lang), callback_data="menu:home")
    widths.append(2)
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
    _leave(builder, lang, to=f"admin:{_SCREEN_PARENT.get('settings', 'home')}")
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
    if screen in _CATEGORY_LABELS:
        return Screen(t(_CATEGORY_LABELS[screen], lang), _category_keyboard(lang, screen))
    if screen == "stats":
        return Screen(await panel.stats_text(pool, lang), _section_keyboard(lang, "stats"))
    if screen == "users":
        return await _users_screen(pool, lang, offset=offset)
    if screen == "broadcast":
        return await _broadcast_screen(pool, lang)
    if screen == "blocks":
        return Screen(await _failure_report(pool, cobalt, lang), _section_keyboard(lang, "blocks"))
    if screen == "groups":
        return Screen(await panel.groups_text(pool, lang), _section_keyboard(lang, "groups"))
    if screen == "texts":
        return _texts_screen(lang)
    if screen == "sources":
        return Screen(
            await panel.sources_text(pool, settings, cobalt, lang),
            _section_keyboard(lang, "sources"),
        )
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
    _leave(builder, lang, to=f"admin:{_SCREEN_PARENT.get('broadcast', 'home')}")
    return Screen(t("admin.broadcast_intro", lang, users=total), builder.as_markup())


async def _support_screen(pool: asyncpg.Pool, lang: str) -> Screen:
    """Where the support button points right now, and how to change it."""
    contact = await database.get_support_contact(pool)
    shown = contact or t("admin.support_none", lang)
    return Screen(
        t("admin.support_intro", lang, contact=escape_html(shown)),
        _support_keyboard(lang, configured=bool(contact)),
    )


# ---------------------------------------------------------------------------
# The texts editor: user-facing messages, by feature, editable at the desk
# ---------------------------------------------------------------------------


async def _show(message: Message, screen: Screen) -> None:
    """Put a whole screen (text *and* buttons) into the message being watched."""
    await _edit(message, screen.text, reply_markup=screen.keyboard)


def _text_key_keyboard(key: str, lang: str) -> InlineKeyboardMarkup:
    """The key screen's buttons alone (for answers under a message of their own)."""
    return _text_key_screen(key, lang).keyboard


def _texts_screen(lang: str) -> Screen:
    """The editor's home: one button per feature category."""
    builder = InlineKeyboardBuilder()
    for category in text_store.category_ids():
        builder.button(
            text=t(text_store.CATEGORY_LABEL_KEYS[category], lang),
            callback_data=f"{TXT_PREFIX}cat:{category}",
        )
    builder.adjust(2)
    _leave(builder, lang, to=f"admin:{_SCREEN_PARENT.get('texts', 'home')}")
    return Screen(t("admin.texts_title", lang), builder.as_markup())


def _page(raw: str) -> int:
    """A page number from callback data — ``0`` for anything that is not one."""
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _texts_keys_screen(category: str, page: int, lang: str) -> Screen:
    """One page of a category's texts — each row its key, marked when edited."""
    keys = text_store.category_keys(category)
    pages = max(1, (len(keys) + TEXTS_PAGE_SIZE - 1) // TEXTS_PAGE_SIZE)
    page = min(page, pages - 1)
    window = keys[page * TEXTS_PAGE_SIZE : (page + 1) * TEXTS_PAGE_SIZE]
    builder = InlineKeyboardBuilder()
    widths: list[int] = []
    for key in window:
        mark = "✏️ " if any(text_store.is_edited(key, code) for code in LANGS) else ""
        builder.button(text=f"{mark}{key}", callback_data=f"{TXT_PREFIX}key:{key}")
        widths.append(1)
    nav = 0
    if page > 0:
        builder.button(
            text=t("admin.btn_prev", lang),
            callback_data=f"{TXT_PREFIX}keys:{category}:{page - 1}",
        )
        nav += 1
    if page < pages - 1:
        builder.button(
            text=t("admin.btn_next", lang),
            callback_data=f"{TXT_PREFIX}keys:{category}:{page + 1}",
        )
        nav += 1
    if nav:
        widths.append(nav)
    builder.button(text=t("menu.back", lang), callback_data="admin:texts")
    builder.button(text=t("admin.btn_home", lang), callback_data="menu:home")
    widths.append(2)
    builder.adjust(*widths)
    return Screen(
        t(
            "admin.texts_category",
            lang,
            category=t(text_store.CATEGORY_LABEL_KEYS[category], lang),
        ),
        builder.as_markup(),
    )


def _text_key_screen(key: str, lang: str) -> Screen:
    """One text: what it says now in both languages, and what may be done to it.

    Both values are shown *escaped* — an admin is reading markup source here,
    and the screen's own markup must not be the edited text's guest.
    """
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("admin.texts_edit_en", lang), callback_data=f"{TXT_PREFIX}edit:en:{key}"
    )
    builder.button(
        text=t("admin.texts_edit_fa", lang), callback_data=f"{TXT_PREFIX}edit:fa:{key}"
    )
    builder.adjust(2)
    builder.button(
        text=f"{t('admin.texts_preview', lang)} EN",
        callback_data=f"{TXT_PREFIX}preview:en:{key}",
    )
    builder.button(
        text=f"{t('admin.texts_preview', lang)} FA",
        callback_data=f"{TXT_PREFIX}preview:fa:{key}",
    )
    builder.adjust(2)
    widths = [2, 2]
    resets = [
        code
        for code in LANGS
        if text_store.is_edited(key, code)
    ]
    if resets:
        for code in resets:
            builder.button(
                text=t(f"admin.texts_reset_{code}", lang),
                callback_data=f"{TXT_PREFIX}reset:{code}:{key}",
            )
        widths.append(len(resets))
    builder.button(
        text=t("menu.back", lang),
        callback_data=f"{TXT_PREFIX}keys:{text_store.category_of(key) or 'start'}:0",
    )
    builder.button(text=t("admin.btn_home", lang), callback_data="menu:home")
    widths.append(2)
    builder.adjust(*widths)
    return Screen(
        t(
            "admin.texts_key_title",
            lang,
            key=key,
            en=escape_html(text_store.effective(key, "en")),
            fa=escape_html(text_store.effective(key, "fa")),
        ),
        builder.as_markup(),
    )


def _text_audit(
    action: str,
    key: str,
    lang: str,
    actor: int,
    before: str | None,
    after: str | None,
) -> str:
    """The audit row for one text change: actor, key, language, before/after.

    The row's own ``created_at`` is the timestamp. ``null`` means the catalogue
    default (there was no override, or there no longer is). Concurrent edits
    are **last-write-wins** over the database upsert — and every write is its
    own row with its own before/after pair, so an overwritten value is never
    lost from the history, only from the present. The values are the texts
    themselves (validated user-facing strings), never secrets.
    """
    return json.dumps(
        {
            "action": action,
            "key": key,
            "lang": lang,
            "actor": actor,
            "before": before,
            "after": after,
        },
        ensure_ascii=False,
    )


@router.callback_query(F.data.startswith(TXT_PREFIX))
async def on_texts_button(
    cb: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """Every texts-editor button: browse, edit, preview, reset.

    One handler and one parser on purpose — the admin check runs exactly once
    per tap, and every payload is validated against the catalogue before it
    names anything (a crafted ``txt:key:…`` can at most name a key that exists:
    locked families are not editable even then).
    """
    if not get_settings().is_admin(cb.from_user.id):
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    parts = (cb.data or "")[len(TXT_PREFIX) :].split(":")
    action = parts[0] if parts else ""

    if action == "cat" and len(parts) == 2 and parts[1] in text_store.category_ids():
        await cb.answer()
        await _show(message, _texts_keys_screen(parts[1], 0, lang))
        return
    if (
        action == "keys"
        and len(parts) == 3
        and parts[1] in text_store.category_ids()
    ):
        await cb.answer()
        await _show(message, _texts_keys_screen(parts[1], _page(parts[2]), lang))
        return
    if not (len(parts) == 2 or (action in ("edit", "preview", "reset") and len(parts) == 3)):
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    code = parts[1] if len(parts) == 3 else ""
    key = parts[-1]
    if key not in MESSAGES or (code and code not in LANGS):
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return

    if action == "key":
        await cb.answer()
        await _show(message, _text_key_screen(key, lang))
        return
    if not text_store.editable(key):
        # A crafted payload naming a locked family gets the honest answer — the
        # catalogue default is not anybody's to replace from here.
        await cb.answer(t("admin.only", lang), show_alert=True)
        return
    if action == "edit":
        await cb.answer()
        await state.set_state(AdminStates.text_edit)
        await state.update_data(txt_key=key, txt_lang=code)
        builder = InlineKeyboardBuilder()
        _leave(builder, lang, to=f"{TXT_PREFIX}key:{key}")
        await _edit(
            message,
            t("admin.texts_prompt", lang, key=key, lang=code),
            reply_markup=builder.as_markup(),
        )
        return
    if action == "preview":
        # Byte-for-byte what a user would receive (placeholders visible — they
        # are runtime values, and the admin must keep them). Telegram refusing
        # the markup is itself the validation verdict.
        try:
            await message.answer(text_store.effective(key, code))
        except TelegramBadRequest:
            await cb.answer(
                t(
                    "admin.texts_invalid",
                    lang,
                    reason=t("admin.texts_reason_markup", lang),
                ),
                show_alert=True,
            )
            return
        await cb.answer()
        await _show(message, _text_key_screen(key, lang))
        return
    if action == "reset":
        before = text_store.override_for(key, code)
        await database.reset_text_override(pool, key, code)
        await text_store.cleared(key, code)
        await database.record_fix_event(
            pool,
            kind="text_reset",
            detail=_text_audit("reset", key, code, int(cb.from_user.id), before, None),
        )
        await cb.answer()
        screen = _text_key_screen(key, lang)
        await _edit(
            message,
            f"{t('admin.texts_reset_done', lang, key=key, lang=code)}\n\n{screen.text}",
            reply_markup=screen.keyboard,
        )
        logger.info("text override reset by admin %s: %s (%s)", cb.from_user.id, key, code)
        return
    await cb.answer(t("admin.stale", lang), show_alert=True)


@router.message(AdminStates.text_edit, ~F.text.startswith("/"))
async def on_text_edit_value(
    message: Message, state: FSMContext, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """The new text itself — validated, proven sendable, then saved.

    Three gates before anything is stored: the value validates (length,
    placeholders the default already has, balanced Telegram markup), then it is
    *sent as a message* — the preview is the proof, byte-for-byte what users
    will receive — and only then does it reach the database and the live
    override layer. "Invalid markup must not break the bot" is enforced by
    Telegram itself, right here, instead of in some future user's chat. Every
    save and reset is an audit row (fix_events).
    """
    user = message.from_user
    admin_id = user.id if user else None
    if not get_settings().is_admin(admin_id):
        await state.clear()
        await message.answer(t("admin.only", lang))
        return
    data = await state.get_data()
    key = str(data.get("txt_key") or "")
    code = str(data.get("txt_lang") or "")
    await state.clear()
    if key not in MESSAGES or code not in LANGS or not text_store.editable(key):
        await message.answer(t("admin.stale", lang))
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer(
            t("admin.texts_prompt", lang, key=key, lang=code),
            reply_markup=_text_key_keyboard(key, lang),
        )
        return
    problem = text_store.validate_text(key, value)
    if problem:
        await message.answer(
            t(
                "admin.texts_invalid",
                lang,
                reason=t(f"admin.texts_reason_{problem}", lang),
            ),
            reply_markup=_text_key_keyboard(key, lang),
        )
        return
    try:
        await message.answer(value)
    except TelegramBadRequest:
        await message.answer(
            t(
                "admin.texts_invalid",
                lang,
                reason=t("admin.texts_reason_markup", lang),
            ),
            reply_markup=_text_key_keyboard(key, lang),
        )
        return
    before = text_store.override_for(key, code)
    await database.set_text_override(pool, key, code, value, updated_by=int(admin_id or 0))
    await text_store.saved(key, code, value)
    await database.record_fix_event(
        pool,
        kind="text_override",
        detail=_text_audit("set", key, code, int(admin_id or 0), before, value),
    )
    await message.answer(
        t("admin.texts_saved", lang, key=key, lang=code),
        reply_markup=_text_key_keyboard(key, lang),
    )
    logger.info("text override saved by admin %s: %s (%s)", admin_id, key, code)


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


@router.message(AdminStates.user_search, ~F.text.startswith("/"))
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


# Not a command: "/cancel" (and every other command) keeps its own handler —
# this router is consulted first now, and a command must never become draft text.
@router.message(AdminStates.broadcast, ~F.text.startswith("/"))
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


@router.message(AdminStates.support, ~F.text.startswith("/"))
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


# ---------------------------------------------------------------------------
# Backup & restore — the configuration, owner only
# ---------------------------------------------------------------------------

_OWNER_ACTIONS = frozenset({BK_BACKUP, BK_RESTORE})


def _restore_keyboard(nonce: str, lang: str) -> InlineKeyboardMarkup:
    """The preview's question: apply exactly what was shown, or walk away.

    The buttons carry only the nonce of the server-side pending restore — what
    is applied is decided here, never by callback data.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.restore_confirm_btn", lang), callback_data=f"{BK_GO}{nonce}")
    builder.button(
        text=t("admin.restore_cancel_btn", lang), callback_data=f"{BK_CANCEL}:{nonce}"
    )
    builder.adjust(1)
    return builder.as_markup()


def _restore_cancel_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The upload prompt's only way out — a flow always has one."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("admin.restore_cancel_btn", lang), callback_data=BK_CANCEL)
    builder.adjust(1)
    return builder.as_markup()


def _backup_file(snapshot: dict[str, Any], created_at: str) -> BufferedInputFile:
    """The backup as a file: readable JSON, named for the moment it was taken."""
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = created_at[:16].replace("-", "").replace(":", "").replace("T", "-")
    return BufferedInputFile(payload, filename=f"bot-backup-{stamp or 'now'}.json")


@router.callback_query(F.data == BK_BACKUP)
async def on_backup_now(
    cb: CallbackQuery, pool: asyncpg.Pool, lang: str = DEFAULT_LANG
) -> None:
    """The configuration, as a file — owner only, and never a secret in it.

    What travels is what an operator edited (see services/backup.py): tokens,
    credentials and the generated runtime caches are excluded before the file
    exists, not edited out of it afterwards.
    """
    if not get_settings().is_owner(cb.from_user.id):
        await cb.answer(t("admin.owner_only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer()
    snapshot = await backup_service.build_backup(pool)
    await message.answer_document(
        _backup_file(snapshot.to_dict(), snapshot.created_at),
        caption=t("admin.backup_caption", lang),
    )


@router.callback_query(F.data == BK_RESTORE)
async def on_restore_start(
    cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG
) -> None:
    """Ask for the file; the next document from this owner is judged, never
    applied — validation first, then a preview, then one explicit click."""
    if not get_settings().is_owner(cb.from_user.id):
        await cb.answer(t("admin.owner_only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    await cb.answer()
    await state.set_state(AdminStates.restore)
    await _edit(
        message,
        t("admin.restore_prompt", lang),
        reply_markup=_restore_cancel_keyboard(lang),
    )


@router.message(AdminStates.restore, F.document)
async def on_restore_upload(
    message: Message,
    state: FSMContext,
    bot: Bot,
    lang: str = DEFAULT_LANG,
) -> None:
    """The uploaded file, judged before it is trusted: schema version, format,
    prohibited fields. Only a file that passes is kept for the confirm step —
    a refused file changes nothing, and says which rule it broke."""
    user = message.from_user
    if not get_settings().is_owner(user.id if user else None):
        await state.clear()
        await message.answer(t("admin.owner_only", lang))
        return
    document = message.document
    if document is None or (document.file_size or 0) > backup_service.MAX_BACKUP_BYTES:
        await message.answer(
            t("admin.restore_invalid", lang, reason="format"),
            reply_markup=_restore_cancel_keyboard(lang),
        )
        return
    raw = await bot.download(document)
    try:
        parsed = backup_service.validate_backup(
            json.loads(raw.read() if raw is not None else b"")
        )
    except backup_service.BackupError as exc:
        await message.answer(
            t("admin.restore_invalid", lang, reason=exc.reason),
            reply_markup=_restore_cancel_keyboard(lang),
        )
        return
    except Exception:
        logger.warning("could not read the uploaded backup", exc_info=True)
        await message.answer(
            t("admin.restore_invalid", lang, reason="format"),
            reply_markup=_restore_cancel_keyboard(lang),
        )
        return
    # The judged payload stays server-side behind a short nonce — the
    # confirmation button carries the nonce and nothing else (owner-bound,
    # expiring; see services/backup.py).
    nonce = backup_service.remember_pending(parsed, owner=int(user.id if user else 0))
    await message.answer(
        t(
            "admin.restore_preview",
            lang,
            texts=len(parsed.texts),
            settings=len(parsed.state),
            created=parsed.created_at or "—",
        ),
        reply_markup=_restore_keyboard(nonce, lang),
    )


@router.message(AdminStates.restore, ~F.text.startswith("/"))
async def on_restore_reprompt(message: Message, lang: str = DEFAULT_LANG) -> None:
    """Not a file, and not a command: ask again — the flow stays open."""
    await message.answer(
        t("admin.restore_prompt", lang), reply_markup=_restore_cancel_keyboard(lang)
    )


@router.callback_query(F.data.startswith(BK_CANCEL))
async def on_restore_cancel(
    cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG
) -> None:
    """Nothing was applied, and the panel says so."""
    if not get_settings().is_owner(cb.from_user.id):
        await cb.answer(t("admin.owner_only", lang), show_alert=True)
        return
    nonce = (cb.data or "")[len(BK_CANCEL) :].lstrip(":")
    if nonce:
        backup_service.drop_pending(nonce, owner=int(cb.from_user.id))
    await state.clear()
    message = cb.message if isinstance(cb.message, Message) else None
    await cb.answer()
    if message is not None:
        await _edit(
            message,
            t("admin.restore_cancelled", lang),
            reply_markup=_done_keyboard(lang),
        )


@router.callback_query(F.data.startswith(BK_GO))
async def on_restore_go(
    cb: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """Apply the file that was previewed — owner only, undoable, atomic.

    The order is the whole safety story, in this exact sequence: the pending
    restore is *atomically consumed* first (two concurrent confirmations can
    never both run), then the emergency backup is built and **sent — awaited —
    before any database work** (no network call ever happens inside the
    transaction), a failed send aborts the whole restore with nothing touched,
    the change is applied inside one strictly scoped transaction (any failure
    rolls it all back), and only after the commit does the text sync run —
    whose failure is reported as what it is (a committed restore with a stale
    cache), never as a rollback that cannot happen.
    """
    if not get_settings().is_owner(cb.from_user.id):
        await cb.answer(t("admin.owner_only", lang), show_alert=True)
        return
    message = cb.message if isinstance(cb.message, Message) else None
    if message is None:
        await cb.answer(t("admin.stale", lang), show_alert=True)
        return
    # 1) Atomic consumption — before anything else. Exactly one confirmation
    #    ever wins; a used, expired or foreign nonce finds nothing here.
    parsed = backup_service.take_pending(
        (cb.data or "")[len(BK_GO) :], owner=int(cb.from_user.id)
    )
    if parsed is None:
        await cb.answer(t("admin.restore_expired", lang), show_alert=True)
        return
    await state.clear()
    await cb.answer()
    # 2–3) The emergency backup: built and *sent*, strictly before the
    #      transaction below. The send is awaited — and if it fails, the
    #      restore aborts right here with the database untouched.
    try:
        emergency = await backup_service.build_backup(pool)
        await message.answer_document(
            _backup_file(emergency.to_dict(), emergency.created_at),
            caption=t("admin.restore_emergency_caption", lang),
        )
    except Exception as exc:
        logger.exception("could not send the emergency backup — restore aborted")
        await message.answer(
            t("admin.restore_aborted", lang, detail=escape_html(str(exc)[:200]))
        )
        return
    # 4) One strictly scoped transaction — no network call inside it; a
    #    failure rolls every section back.
    try:
        await backup_service.apply_backup(pool, parsed)
    except Exception as exc:
        logger.exception("restore failed and was rolled back")
        await message.answer(
            t("admin.restore_failed", lang, detail=escape_html(str(exc)[:200]))
        )
        return
    # 5) Committed. The sync runs *after* the commit: if it fails, the restore
    #    is not rolled back (it cannot be) — say so plainly instead.
    try:
        await text_store.restored(await database.text_overrides(pool))
    except Exception as exc:
        logger.exception("restore committed but the text sync failed")
        await message.answer(
            t("admin.restore_sync_failed", lang, detail=escape_html(str(exc)[:200]))
        )
        return
    await message.answer(
        t(
            "admin.restore_done",
            lang,
            texts=len(parsed.texts),
            settings=len(parsed.state),
        )
    )
    logger.info(
        "restore applied by owner %s: %d text(s), %d setting(s)",
        cb.from_user.id,
        len(parsed.texts),
        len(parsed.state),
    )
