"""User-facing handlers: start, profile, premium, help, language, link intake.

The Gateway never downloads anything — it validates the link, checks limits and
the smart cache, then pushes a ``DownloadTask`` into the queue.

Three things about the UI are deliberate. Everything is said in the user's own
language (resolved once by the middleware, carried into the queue so the *worker*'s
messages match). Every screen is *one* message that gets edited in place, so a user
who taps around never collects a stack of menus. And the buttons are drawn from the
link itself (``services/content.py``): a photo post is not offered a 1080p tier, a
YouTube video is not offered "send the photos", and neither is offered an audio
format it cannot produce.

Anything that costs a wait says so immediately, because a bot that looks frozen is a
bot people tap twice.
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
from core.i18n import (
    DEFAULT_LANG,
    language_options,
    normalize_lang,
    normalize_supported,
    t,
)
from core.utils import escape_html, extract_url, today_local, validate_url
from handlers.payment import callback_message, plans_keyboard
from services import cache as cache_service
from services import content, preflight, spotify
from services.delivery import send_cached_file
from services.extractor import ExtractorService
from services.queue import DownloadTask, TaskQueue
from services.subscription import effective_daily_limit, is_admin, is_premium_active

logger = logging.getLogger(__name__)
router = Router(name="user")

#: The callback prefix for a format choice: ``fmt:<media_format>:<quality>``.
FMT_PREFIX = "fmt:"
LANG_PREFIX = "lang:"


class DownloadStates(StatesGroup):
    """The one step a user is in: a link is known, its format is not yet."""

    waiting_format = State()


def _main_menu(lang: str) -> InlineKeyboardMarkup:
    """The three things a user can do here, plus the way to change the language."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.profile", lang), callback_data="menu:profile")
    builder.button(text=t("menu.premium", lang), callback_data="menu:premium")
    builder.button(text=t("menu.help", lang), callback_data="menu:help")
    builder.button(text=t("menu.language", lang), callback_data="menu:language")
    builder.adjust(1)  # the labels are long; one per row stays tappable on a phone
    return builder.as_markup()


def _back_to_menu(lang: str) -> InlineKeyboardMarkup:
    """Every screen but the menu itself carries its way back."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def _format_keyboard(url: str, lang: str) -> InlineKeyboardMarkup:
    """What can be asked for *this* link — and nothing else.

    One button per offered choice, in the order ``services/content.py`` puts them
    (best first for video, the untouched stream first for audio), and a way back to
    the menu so a user who changed their mind is not stuck in a question.
    """
    builder = InlineKeyboardBuilder()
    for choice in content.routing_for(url).choices:
        builder.button(
            text=t(choice.label_key, lang),
            callback_data=f"{FMT_PREFIX}{choice.media_format}:{choice.quality}",
        )
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def _language_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The two (or more) languages this bot speaks, each naming itself."""
    builder = InlineKeyboardBuilder()
    for code, label in language_options():
        marker = "✅ " if code == normalize_lang(lang) else ""
        builder.button(text=f"{marker}{label}", callback_data=f"{LANG_PREFIX}{code}")
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def _welcome_text(name: str, lang: str) -> str:
    """The first screen: what this bot does, in one glance, then the menu.

    HTML on purpose (the bot's default parse mode) — the title and the service list
    carry the emphasis, so the buttons under it read as the actions rather than as
    more text. The services are *named*, because "supports many sites" is not an
    answer to "can it do the one I have".
    """
    return "\n".join(
        (
            t("start.welcome", lang, name=escape_html(name)),
            "",
            t("menu.language_hint", lang),
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
async def cmd_start(message: Message, user: asyncpg.Record, lang: str = DEFAULT_LANG) -> None:
    await message.answer(
        _welcome_text(user["username"] or t("misc.friend", lang), lang),
        reply_markup=_main_menu(lang),
    )


@router.callback_query(F.data == "menu:home")
async def on_menu_home(
    cb: CallbackQuery, user: asyncpg.Record, lang: str = DEFAULT_LANG
) -> None:
    """The "back" button — the screen it came from becomes the menu again.

    Edited, not re-sent: a user who taps around should end up with one menu, not a
    pile of them (and Telegram's own "message is not modified" answer is handled by
    the reply fallback).
    """
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(
        message,
        _welcome_text(user["username"] or t("misc.friend", lang), lang),
        reply_markup=_main_menu(lang),
    )


@router.callback_query(F.data == "menu:profile")
async def on_menu_profile(
    cb: CallbackQuery,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    lang: str = DEFAULT_LANG,
) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(
        message,
        await _profile_text(user, lang, pool, queue),
        reply_markup=_back_to_menu(lang),
    )


@router.callback_query(F.data == "menu:premium")
async def on_menu_premium(
    cb: CallbackQuery, pool: asyncpg.Pool, user: asyncpg.Record, lang: str = DEFAULT_LANG
) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    await _send_premium(message, pool, user, lang, edit=True)


@router.callback_query(F.data == "menu:help")
async def on_menu_help(cb: CallbackQuery, lang: str = DEFAULT_LANG) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(message, _help_text(lang), reply_markup=_back_to_menu(lang))


@router.callback_query(F.data == "menu:language")
async def on_menu_language(cb: CallbackQuery, lang: str = DEFAULT_LANG) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(
        message, t("language.title", lang), reply_markup=_language_keyboard(lang)
    )


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------

@router.message(Command("language"))
async def cmd_language(
    message: Message,
    command: CommandObject,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """``/language`` shows the picker; ``/language fa`` sets it in one step."""
    argument = (command.args or "").strip()
    if not argument:
        await message.answer(t("language.title", lang), reply_markup=_language_keyboard(lang))
        return
    chosen = _supported_language(argument)
    if chosen is None:
        options = ", ".join(code for code, _ in language_options())
        await message.answer(t("misc.unknown_language", lang, options=options))
        return
    await database.set_user_language(pool, user["telegram_id"], chosen)
    await message.answer(
        t("language.set", chosen, name=_label(chosen)),
        reply_markup=_main_menu(chosen),
    )


def _label(code: str) -> str:
    """The picker label for a language code (``🇬🇧 English``)."""
    for candidate, label in language_options():
        if candidate == code:
            return label
    return code


def _supported_language(value: str) -> str | None:
    """The language a string names, or ``None`` when it names none of ours.

    ``normalize_supported`` (not ``normalize_lang``) on purpose: a typo like
    ``/language du`` must be *rejected*, not silently turned into the default.
    """
    return normalize_supported(value)


@router.callback_query(F.data.startswith(LANG_PREFIX))
async def on_language_chosen(
    cb: CallbackQuery,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    """A language tap: store it, then answer *in the new language*.

    The confirmation is the proof: a user who taps فارسی and still reads English
    would have to tap again to find out whether it worked.
    """
    chosen = _supported_language((cb.data or "")[len(LANG_PREFIX) :])
    if chosen is None:
        options = ", ".join(code for code, _ in language_options())
        await cb.answer(t("misc.unknown_language", lang, options=options), show_alert=True)
        return
    await database.set_user_language(pool, user["telegram_id"], chosen)
    await cb.answer(t("language.set", chosen, name=_label(chosen)))
    message = callback_message(cb)
    if message is None:
        return
    await _edit_or_reply(
        message,
        _welcome_text(user["username"] or t("misc.friend", chosen), chosen),
        reply_markup=_main_menu(chosen),
    )


# ---------------------------------------------------------------------------
# Profile / premium / help (the menu's screens)
# ---------------------------------------------------------------------------

@router.message(Command("profile"))
async def cmd_profile(
    message: Message,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    lang: str = DEFAULT_LANG,
) -> None:
    await message.answer(
        await _profile_text(user, lang, pool, queue), reply_markup=_back_to_menu(lang)
    )


@router.message(Command("premium"))
async def cmd_premium(
    message: Message,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    lang: str = DEFAULT_LANG,
) -> None:
    await _send_premium(message, pool, user, lang)


@router.message(Command("help"))
async def cmd_help(message: Message, lang: str = DEFAULT_LANG) -> None:
    await message.answer(_help_text(lang), reply_markup=_back_to_menu(lang))


def _help_text(lang: str) -> str:
    """What the help screen says: how to use the bot, then the commands."""
    return "\n".join((t("help.title", lang), "", t("help.body", lang)))


async def _send_premium(
    message: Message,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    lang: str,
    *,
    edit: bool = False,
) -> None:
    """The VIP pitch with the plans under it (and the way back to the menu)."""
    settings = get_settings()
    text = t(
        "premium.title",
        lang,
        free=settings.default_daily_limit,
        premium=settings.premium_daily_limit,
    )
    if is_admin(user):
        # An admin cannot buy anything they do not already have; saying so beats
        # letting them pay for a status they hold.
        text = f"{text}\n\n{t('premium.admin_note', lang)}"
    keyboard = await plans_keyboard(pool, lang=lang, back=True)
    if keyboard is None:
        await _reply_or_edit(message, edit, t("pay.no_plans", lang))
        return
    await _reply_or_edit(message, edit, text, reply_markup=keyboard)


async def _reply_or_edit(message: Message, edit: bool, text: str, **kwargs: Any) -> None:
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
    message: Message,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    lang: str = DEFAULT_LANG,
) -> None:
    await message.answer(await _profile_text(user, lang, pool, queue))


def _status_line(user: asyncpg.Record, lang: str) -> str:
    """Free 🪙, VIP 💎, or the admin's own state — with what is left of it."""
    if is_admin(user):
        return t("profile.status.admin", lang)
    if not is_premium_active(user):
        return t("profile.status.free", lang)
    until = user["premium_until"]
    if until is None:
        return t("profile.status.premium_lifetime", lang)
    days = max((until - datetime.now(timezone.utc)).days, 0)
    return t("profile.status.premium_days", lang, days=days, date=f"{until:%Y-%m-%d}")


def _quota_line(user: asyncpg.Record, lang: str, used: int) -> str:
    """The quota, honestly: a numbered ceiling, or ♾ for an admin's."""
    limit = effective_daily_limit(user)
    if is_admin(user):
        return t("profile.quota_unlimited", lang, used=used)
    if used >= limit:
        return t("profile.quota_used_up", lang, used=used, limit=limit)
    return t("profile.quota_left", lang, used=used, limit=limit, left=limit - used)


async def _profile_text(
    user: asyncpg.Record, lang: str, pool: asyncpg.Pool, queue: TaskQueue
) -> str:
    """The account, in the order a user asks about it: who, which plan, how much.

    Shared by ``/profile`` and ``/status`` on purpose: two screens telling slightly
    different stories about the same quota is how support questions start.
    """
    usage = await database.get_daily_usage(pool, user["telegram_id"])
    used = (
        usage["daily_downloads"]
        if usage and usage["last_download_date"] == today_local()
        else 0
    )
    depth = await queue.depth()
    username = f"@{user['username']}" if user["username"] else "—"
    return "\n".join(
        (
            t("profile.title", lang),
            "",
            t("profile.id", lang, telegram_id=user["telegram_id"]),
            t("profile.username", lang, username=escape_html(username)),
            t("profile.status", lang, status=_status_line(user, lang)),
            _quota_line(user, lang, used),
            t("profile.queue", lang, depth=depth),
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
    lang: str = DEFAULT_LANG,
) -> None:
    current = await state.get_state()
    if current == DownloadStates.waiting_format.state:
        # User pasted a new URL while a format choice was pending → replace it.
        url = extract_url(message.text or "")
        if url and validate_url(url):
            await state.update_data(url=url)
            await message.answer(
                _media_question(url, lang), reply_markup=_format_keyboard(url, lang)
            )
        else:
            await message.answer(t("intake.invalid_link", lang))
        return
    if current is not None:
        await message.answer(t("intake.step_in_progress", lang))
        return

    url = extract_url(message.text or "")
    if not url:
        await message.answer(t("intake.no_link_found", lang))
        return
    await _queue_url_flow(message, state, user, url, lang)


@router.message(Command("download"))
async def cmd_download(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    lang: str = DEFAULT_LANG,
) -> None:
    url = extract_url(command.args or "")
    if not url and message.reply_to_message:
        url = extract_url(
            message.reply_to_message.text or message.reply_to_message.caption or ""
        )
    if not url:
        await message.answer(t("intake.download_usage", lang))
        return
    await _queue_url_flow(message, state, user, url, lang)


async def _queue_url_flow(
    message: Message,
    state: FSMContext,
    user: asyncpg.Record,
    url: str,
    lang: str,
) -> None:
    # The first thing the user sees: work has started. The probe below is quick,
    # but "quick" and "instant" look very different in a chat window — and the
    # message it starts in is the one that ends up carrying the outcome.
    status = await message.answer(t("intake.analyse", lang))
    if not validate_url(url):
        await _edit_or_reply(status, t("intake.invalid_link", lang))
        return
    if not await _probe_supported(url):
        await _edit_or_reply(status, t("intake.unsupported", lang))
        return
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(url=url)
    await _edit_or_reply(status, _media_question(url, lang), reply_markup=_format_keyboard(url, lang))


def _media_question(url: str, lang: str) -> str:
    """The question this link deserves — quality, audio format, or its media."""
    return t(content.routing_for(url).header_key, lang)


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


@router.callback_query(DownloadStates.waiting_format, F.data.startswith(FMT_PREFIX))
async def on_format_chosen(
    cb: CallbackQuery,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    bot: Bot,
    lang: str = DEFAULT_LANG,
) -> None:
    data = await state.get_data()
    url = data.get("url")
    if not url:
        await cb.answer(t("intake.link_expired", lang), show_alert=True)
        await state.clear()
        return
    media_format, quality = _parse_format(cb.data)
    choice = (
        content.find_choice(url, media_format, quality) if media_format and quality else None
    )
    if choice is None:
        # A tap that was never offered (an older menu, a forwarded message, a
        # crafted callback): nothing is queued, and the question is asked again.
        await cb.answer(t("intake.no_format", lang), show_alert=True)
        return
    await state.clear()

    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    chat_id = message.chat.id
    # Said before the checks, not after them: the cache lookup, the quota read and
    # the preflight are three round trips, and a tap that produces nothing for a
    # second reads as a broken button.
    status = await message.answer(t("intake.queueing", lang))

    # 1) Smart cache hit → resend the previous file_id instantly, no re-download.
    #    The key includes the requested format *and tier*, so an MP3 ask never gets
    #    a video, and a 480p ask never replays the 1080p file.
    cached = await cache_service.get_cached(pool, url, choice.media_format, choice.quality)
    if cached is not None:
        await cb.answer()
        if await send_cached_file(bot, chat_id, cached, caption=t("work.cache_caption", lang)):
            await _edit_or_reply(status, t("intake.cache_hit", lang))
            return
        # dead file_id → drop it and fall through to a real download
        await cache_service.forget(pool, url, choice.media_format, choice.quality)

    # 2) Soft daily-quota check (the worker claims the slot atomically).
    usage = await database.get_daily_usage(pool, user["telegram_id"])
    used = usage["daily_downloads"] if usage and usage["last_download_date"] == today_local() else 0
    limit = effective_daily_limit(user)
    if used >= limit:
        await _edit_or_reply(status, t("intake.quota_exhausted", lang, used=used, limit=limit))
        await cb.answer(t("intake.quota_exhausted_alert", lang), show_alert=True)
        return

    # 3) Preflight: a YouTube link that cannot work should not cost a wait. Only
    #    what is *known* refuses the link; a suspicion rides along as a note — and
    #    a fallback engine turns even the known case into a note, because it may
    #    still serve the file (see services/fallback.py).
    settings = get_settings()
    verdict = preflight.youtube_preflight(
        url,
        settings.cookie_file,
        lang=lang,
        fallback_available=settings.cobalt_enabled,
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
        media_format=choice.media_format,
        quality=choice.quality,
        lang=lang,
        url_hash=cache_service.cache_key(url, choice.media_format, choice.quality),
    )
    depth = await queue.enqueue(task)
    await cb.answer()
    lines = [
        t("intake.queued", lang, depth=depth),
        t("intake.queued_background", lang),
    ]
    if spotify.is_spotify_url(url):
        lines.append(t("intake.spotify_note", lang))
    if verdict.message:  # a warning, not a refusal: the link is queued either way
        lines.append(verdict.message)
    await _edit_or_reply(status, "\n".join(lines))


def _parse_format(data: str | None) -> tuple[str, str]:
    """``fmt:<media_format>:<quality>`` → ``(media_format, quality)``; empty on junk."""
    parts = (data or "").split(":")
    if len(parts) != 3:
        return ("", "")
    return (parts[1], parts[2])


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext, lang: str = DEFAULT_LANG) -> None:
    await state.clear()
    await message.answer(t("intake.cancelled", lang))
