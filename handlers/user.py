"""User-facing handlers: start, profile, premium, help, link intake, queueing.

The Gateway never downloads anything — it validates the link, checks limits and
the smart cache, then pushes a ``DownloadTask`` into the queue.

Two things about the UI are deliberate. Every screen is *one* message that gets
edited in place, so a user who taps around never collects a stack of menus; and
anything that costs a wait (analysis, queueing) says so immediately, because a
bot that looks frozen is a bot people tap twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core.config import get_settings
from core.utils import MediaFormat, escape_html, extract_url, today_local, validate_url
from handlers.payment import NO_PLANS_TEXT, callback_message, plans_keyboard
from services import cache as cache_service
from services import preflight, spotify
from services.delivery import send_cached_file
from services.extractor import ExtractorService
from services.queue import DownloadTask, TaskQueue
from services.subscription import effective_daily_limit, is_premium_active

logger = logging.getLogger(__name__)
router = Router(name="user")

_STALE_CALLBACK = "⚠️ این پیام دیگه در دسترس نیست؛ لطفاً لینک رو دوباره بفرست."

#: Shown the moment work starts, so nothing looks frozen (see on_format_chosen).
_ANALYSING = "🔍 در حال تحلیل لینک…"
_QUEUEING = "🔍 در حال تحلیل و ارسال به صف پردازش... ⏳"

#: A Spotify link is never downloaded *from* Spotify: it is rewritten to the same
#: song on YouTube (see services/spotify.py). Saying so before the wait is what
#: keeps the file that arrives from looking like the wrong one.
_SPOTIFY_NOTE = "🎵 لینک اسپاتیفای است — همین آهنگ از نسخهٔ یوتیوب دانلود میشود."


class DownloadStates(StatesGroup):
    waiting_format = State()


def _format_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎬 ویدیو (بهترین کیفیت)", callback_data="fmt:video")
    builder.button(text="🎵 فقط صدا (MP3)", callback_data="fmt:audio")
    builder.adjust(1)
    return builder.as_markup()


def _main_menu() -> InlineKeyboardMarkup:
    """The three things a user can do here; everything else is a command."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 پروفایل من", callback_data="menu:profile")
    builder.button(text="💎 ارتقا به ویژه (VIP)", callback_data="menu:premium")
    builder.button(text="❓ راهنما", callback_data="menu:help")
    builder.adjust(1)  # the labels are long; one per row stays tappable on a phone
    return builder.as_markup()


def _back_to_menu() -> InlineKeyboardMarkup:
    """Every screen but the menu itself carries its way back."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 بازگشت", callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def _welcome_text(name: str) -> str:
    """The first screen: what this bot does, in one glance, then the menu.

    HTML on purpose (the bot's default parse mode) — the title and the site list
    carry the emphasis, so the three buttons under it read as the actions rather
    than as more text.
    """
    return "\n".join(
        (
            f"<b>سلام {escape_html(name)} 👋</b>",
            "",
            "🎬 <b>دانلودر حرفه‌ای</b> — فقط لینک را بفرست، بقیه‌اش با من:",
            "▫️ یوتیوب • توییتر/X • اینستاگرام • تیک‌تاک",
            "▫️ فیسبوک • ریدیت • ساندکلاود • و ده‌ها سرویس دیگر",
            "",
            "⚙️ دانلود در پس‌زمینه انجام می‌شود و فایل <b>همین‌جا</b> برایت ارسال می‌شود؛ "
            "پیشرفت را در همان پیام می‌بینی، و اگر لینکی دانلود نشد علتش گفته می‌شود.",
            "",
            "👇 از دکمه‌های زیر شروع کن:",
        )
    )


async def _edit_or_reply(message: Message, text: str, **kwargs: Any) -> None:
    """Update a message in place; one that cannot be edited gets a fresh reply.

    Telegram refuses edits to old messages and to identical content, and a menu
    that fails silently is worse than a new message — so the fallback is a send.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        await message.answer(text, **kwargs)


# ---------------------------------------------------------------------------
# Start / menu
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, user: asyncpg.Record) -> None:
    await message.answer(
        _welcome_text(user["username"] or "دوست عزیز"), reply_markup=_main_menu()
    )


@router.callback_query(F.data == "menu:home")
async def on_menu_home(cb: CallbackQuery, user: asyncpg.Record) -> None:
    """"🔙 بازگشت" — the screen the button came from becomes the menu again.

    Edited, not re-sent: a user who taps around should end up with one menu, not a
    pile of them (and Telegram's own "message is not modified" answer is handled by
    the reply fallback).
    """
    message = callback_message(cb)
    if message is None:
        await cb.answer(_STALE_CALLBACK, show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(
        message,
        _welcome_text(user["username"] or "دوست عزیز"),
        reply_markup=_main_menu(),
    )


@router.callback_query(F.data == "menu:profile")
async def on_menu_profile(
    cb: CallbackQuery, user: asyncpg.Record, pool: asyncpg.Pool, queue: TaskQueue
) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(_STALE_CALLBACK, show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(
        message, await _profile_text(user, pool, queue), reply_markup=_back_to_menu()
    )


@router.callback_query(F.data == "menu:premium")
async def on_menu_premium(cb: CallbackQuery, pool: asyncpg.Pool) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(_STALE_CALLBACK, show_alert=True)
        return
    await cb.answer()
    await _send_premium(message, pool, edit=True)


@router.callback_query(F.data == "menu:help")
async def on_menu_help(cb: CallbackQuery) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(_STALE_CALLBACK, show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(message, _HELP_TEXT, reply_markup=_back_to_menu())


# ---------------------------------------------------------------------------
# Profile / premium / help (the menu's three screens)
# ---------------------------------------------------------------------------

@router.message(Command("profile"))
async def cmd_profile(
    message: Message, user: asyncpg.Record, pool: asyncpg.Pool, queue: TaskQueue
) -> None:
    await message.answer(
        await _profile_text(user, pool, queue), reply_markup=_back_to_menu()
    )


@router.message(Command("premium"))
async def cmd_premium(message: Message, pool: asyncpg.Pool) -> None:
    await _send_premium(message, pool)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(_HELP_TEXT, reply_markup=_back_to_menu())


#: What the help screen says: how to use the bot, then the commands worth knowing.
_HELP_TEXT = (
    "❓ <b>راهنما</b>\n\n"
    "1️⃣ لینک رو بفرست (یوتیوب، اینستاگرام، تیک‌تاک، توییتر/X، فیسبوک، ریدیت، ساندکلاود و …).\n"
    "2️⃣ انتخاب کن: 🎬 ویدیو با بهترین کیفیت یا 🎵 فقط صدا (MP3).\n"
    "3️⃣ دانلود در پس‌زمینه انجام می‌شه و فایل همین‌جا برات فرستاده می‌شه — می‌تونی همون‌جا "
    "منتظر بمونی یا بری، خبرش می‌رسه.\n\n"
    "پیشرفت دانلود را در همان پیام می‌بینی، و اگر لینکی دانلود نشد علتش گفته می‌شه.\n"
    "لینک خصوصی، پخش زنده و playlist دانلود نمی‌شه.\n\n"
    "<b>دستورها</b>\n"
    "• /profile — پروفایل، وضعیت و سهمیهٔ امروز\n"
    "• /premium — اشتراک ویژه (VIP)\n"
    "• /status — خلاصهٔ وضعیت حساب\n"
    "• /cancel — لغو مرحلهٔ فعلی\n"
    "• /start — همین منو\n\n"
    "هر دانلود یک واحد از سهمیهٔ روزانه کم می‌کند؛ با /premium سهمیه چند برابر می‌شود."
)


async def _send_premium(message: Message, pool: asyncpg.Pool, *, edit: bool = False) -> None:
    """The VIP pitch with the plans under it (and the way back to the menu)."""
    settings = get_settings()
    text = (
        "💎 <b>اشتراک ویژه (VIP)</b>\n\n"
        f"سهمیهٔ روزانه: {settings.default_daily_limit} → <b>{settings.premium_daily_limit}</b> "
        "دانلود\n"
        "صف با اولویت، بدون تبلیغ، برای آرشیو و استفادهٔ روزمره.\n\n"
        "تعرفه‌ها 👇"
    )
    keyboard = await plans_keyboard(pool, back=True)
    if keyboard is None:
        await _reply_or_edit(message, edit, NO_PLANS_TEXT)
        return
    await _reply_or_edit(message, edit, text, reply_markup=keyboard)


async def _reply_or_edit(
    message: Message, edit: bool, text: str, **kwargs: Any
) -> None:
    """One screen, whichever way we got here (a command or a menu button)."""
    if edit:
        await _edit_or_reply(message, text, **kwargs)
    else:
        await message.answer(text, **kwargs)


# ---------------------------------------------------------------------------
# Status / profile text
# ---------------------------------------------------------------------------

@router.message(Command("status"))
async def cmd_status(
    message: Message, user: asyncpg.Record, pool: asyncpg.Pool, queue: TaskQueue
) -> None:
    await message.answer(await _profile_text(user, pool, queue))


def _status_line(user: asyncpg.Record) -> str:
    """رایگان 🪙 or ویژه 💎, with what is left of it."""
    if not is_premium_active(user):
        return "وضعیت: رایگان 🪙"
    until = user["premium_until"]
    if until is None:
        return "وضعیت: ویژه 💎 — دائمی"
    days = max((until - datetime.now(timezone.utc)).days, 0)
    return f"وضعیت: ویژه 💎 — {days} روز مانده (تا {until:%Y-%m-%d})"


async def _profile_text(
    user: asyncpg.Record, pool: asyncpg.Pool, queue: TaskQueue
) -> str:
    """The account, in the order a user asks about it: who, which plan, how much.

    Shared by ``/profile`` and ``/status`` on purpose: two screens telling slightly
    different stories about the same quota is how support questions start.
    """
    usage = await database.get_daily_usage(pool, user["telegram_id"])
    limit = effective_daily_limit(user)
    used = usage["daily_downloads"] if usage and usage["last_download_date"] == today_local() else 0
    depth = await queue.depth()
    username = f"@{user['username']}" if user["username"] else "—"
    quota = f"📥 سهمیهٔ امروز: {used} از {limit}"
    quota += f" — {limit - used} باقی مانده" if used < limit else " — تمام شد"
    return "\n".join(
        (
            "👤 <b>پروفایل من</b>",
            "",
            f"🆔 شناسهٔ تلگرام: <code>{user['telegram_id']}</code>",
            f"🔗 نام کاربری: {escape_html(username)}",
            f"💎 {_status_line(user)}",
            quota,
            f"🕒 کارهای در صف: {depth}",
        )
    )


# ---------------------------------------------------------------------------
# URL intake → format choice → queue
# ---------------------------------------------------------------------------

@router.message(F.text, ~F.text.startswith("/"))
async def on_text_with_url(
    message: Message,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
) -> None:
    current = await state.get_state()
    if current == DownloadStates.waiting_format.state:
        # User pasted a new URL while a format choice was pending → replace it.
        url = extract_url(message.text or "")
        if url and validate_url(url):
            await state.update_data(url=url)
            await message.answer("لینک به‌روزرسانی شد؛ کیفیت رو انتخاب کن 👇", reply_markup=_format_keyboard())
        else:
            await message.answer("❌ لینک نامعتبر است.")
        return
    if current is not None:
        await message.answer("یک مرحلهٔ دیگه هنوز در جریانه؛ با /cancel از نو شروع کن.")
        return

    url = extract_url(message.text or "")
    if not url:
        await message.answer(
            "لینکی در پیامت پیدا نکردم. یک URL کامل بفرست "
            "(مثل https://youtube.com/watch?v=...) یا /cancel"
        )
        return
    await _queue_url_flow(message, state, pool, user, url)


@router.message(Command("download"))
async def cmd_download(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
) -> None:
    url = extract_url(command.args or "")
    if not url and message.reply_to_message:
        url = extract_url(
            message.reply_to_message.text or message.reply_to_message.caption or ""
        )
    if not url:
        await message.answer("استفاده: /download <لینک> — یا لینک رو مستقیم بفرست.")
        return
    await _queue_url_flow(message, state, pool, user, url)


async def _queue_url_flow(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    url: str,
) -> None:
    # The first thing the user sees: work has started. The probe below is quick,
    # but "quick" and "instant" look very different in a chat window — and the
    # message it starts in is the one that ends up carrying the outcome.
    status = await message.answer(_ANALYSING)
    if not validate_url(url):
        await _edit_or_reply(status, "❌ لینک نامعتبر است. لینک باید با http:// یا https:// شروع شود.")
        return
    if not await _probe_supported(url):
        await _edit_or_reply(status, "❌ این لینک توسط موتور استخراج پشتیبانی نمی‌شود.")
        return
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(url=url)
    await _edit_or_reply(status, "چی می‌خوای؟ 👇", reply_markup=_format_keyboard())


async def _probe_supported(url: str) -> bool:
    """Cheap offline probe via yt-dlp extractors; never blocks the user."""
    if spotify.is_spotify_url(url):
        # Served by rewriting the link, not by yt-dlp (see services/spotify.py), so
        # yt-dlp's opinion of it — "known to use DRM protection" — decides nothing.
        return True
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(ExtractorService.is_url_supported, url),
            timeout=2.0,
        )
    except Exception:
        return True  # probe failure must not block the user — worker will decide


@router.callback_query(DownloadStates.waiting_format, F.data.in_({"fmt:video", "fmt:audio"}))
async def on_format_chosen(
    cb: CallbackQuery,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    bot: Bot,
) -> None:
    data = await state.get_data()
    url = data.get("url")
    if not url:
        await cb.answer("لینک منقضی شد؛ دوباره لینک رو بفرست.", show_alert=True)
        await state.clear()
        return
    media_format: MediaFormat = "audio" if cb.data == "fmt:audio" else "video"
    await state.clear()

    message = callback_message(cb)
    if message is None:
        await cb.answer(_STALE_CALLBACK, show_alert=True)
        return
    chat_id = message.chat.id
    # Said before the checks, not after them: the cache lookup, the quota read and
    # the preflight are three round trips, and a tap that produces nothing for a
    # second reads as a broken button.
    status = await message.answer(_QUEUEING)

    # 1) Smart cache hit → resend the previous file_id instantly, no re-download.
    #    The key includes the requested format, so an MP3 ask never gets a video.
    cached = await cache_service.get_cached(pool, url, media_format)
    if cached is not None:
        await cb.answer()
        if await send_cached_file(bot, chat_id, cached):
            await _edit_or_reply(
                status, "⚡️ این لینک قبلاً دانلود شده — فایل از حافظهٔ کش ارسال شد."
            )
            return
        await cache_service.forget(pool, url, media_format)  # dead file_id → fall through

    # 2) Soft daily-quota check (the worker claims the slot atomically).
    usage = await database.get_daily_usage(pool, user["telegram_id"])
    used = usage["daily_downloads"] if usage and usage["last_download_date"] == today_local() else 0
    limit = effective_daily_limit(user)
    if used >= limit:
        await _edit_or_reply(
            status,
            f"⛔️ سهمیهٔ دانلود امروزت ({used} از {limit}) تمام شده است — با /premium سهمیه‌ات "
            "را بیشتر کن.",
        )
        await cb.answer("⛔️ سهمیهٔ روزانه تمام شده است.", show_alert=True)
        return

    # 3) Preflight: a YouTube link that cannot work should not cost a wait. Only
    #    what is *known* refuses the link; a suspicion rides along as a note — and
    #    a fallback engine turns even the known case into a note, because it may
    #    still serve the file (see services/fallback.py).
    settings = get_settings()
    verdict = preflight.youtube_preflight(
        url, settings.cookie_file, fallback_available=settings.cobalt_enabled
    )
    if verdict.refused:
        await cb.answer()
        await _edit_or_reply(status, verdict.message)
        return

    # 4) Push to the queue — the hard work happens in background workers.
    task = DownloadTask(
        url=url,
        telegram_id=user["telegram_id"],
        chat_id=chat_id,
        media_format=media_format,
        url_hash=cache_service.cache_key(url, media_format),
    )
    depth = await queue.enqueue(task)
    await cb.answer()
    lines = [
        f"⏳ لینک در صف پردازش قرار گرفت (موقعیت تقریبی: {depth}).",
        "دانلود و ارسال در پس‌زمینه انجام می‌شه — همین‌جا خبرت می‌کنیم.",
    ]
    if spotify.is_spotify_url(url):
        lines.append(_SPOTIFY_NOTE)
    if verdict.message:  # a warning, not a refusal: the link is queued either way
        lines.append(verdict.message)
    await _edit_or_reply(status, "\n".join(lines))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("✅ لغو شد.")
