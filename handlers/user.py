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
import re
from datetime import datetime, timezone
from typing import Any

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core.config import get_settings
from core.i18n import (
    DEFAULT_LANG,
    lang_button,
    language_options,
    normalize_lang,
    normalize_supported,
    t,
)
from core.ui import callback_message
from core.ui import edit_or_reply as _edit_or_reply
from core.utils import escape_html, extract_url, today_local, validate_url
from handlers.payment import plans_keyboard
from services import cache as cache_service
from services import content, preflight, spotify
from services.delivery import replay_caption, send_cached_file
from services.extractor import ExtractorService
from services.queue import DownloadTask, TaskQueue
from services.subscription import effective_daily_limit, is_admin, is_premium_active

logger = logging.getLogger(__name__)
router = Router(name="user")

#: The callback prefix for a final choice: ``fmt:<media_format>:<quality>``.
FMT_PREFIX = "fmt:"
#: The callback prefix for the audio menu's first step: ``audf:<codec>`` (open
#: that codec's quality presets) — ``audf:back`` returns to the question.
AUDF_PREFIX = "audf:"
LANG_PREFIX = "lang:"


class DownloadStates(StatesGroup):
    """The one step a user is in: a link is known, its format is not yet."""

    waiting_format = State()


def _main_menu(lang: str, *, admin: bool = False) -> InlineKeyboardMarkup:
    """HOME — the navigation hub everything hangs off: Download, Profile, Help.

    Deliberate shape, not a flat list. Download gets the top row to itself: it is
    why most people came. Premium, Language and the support contact live under
    Profile and Help now — they are account or help concerns, and a home carrying
    every action is a wall of buttons. And the admin panel button is drawn for
    admins only (an admin is never offered «💎 Go VIP» either: they hold it
    permanently, so the button could only lead to a screen explaining that they
    cannot buy what they already have).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.download", lang), callback_data="menu:download")
    builder.button(text=t("menu.profile", lang), callback_data="menu:profile")
    builder.button(text=t("menu.help", lang), callback_data="menu:help")
    if admin:
        # The panel used to be reachable only by remembering that `/admin` exists:
        # an operator had no way to tell a missing permission from a missing
        # feature. It is a button now, on the one screen they always open.
        builder.button(text=t("menu.admin", lang), callback_data="menu:admin")
    builder.adjust(1, 2, 1)
    return builder.as_markup()


def _menu_for(user: asyncpg.Record, lang: str) -> InlineKeyboardMarkup:
    """The home screen as this user should see it (admins get the panel button)."""
    return _main_menu(lang, admin=is_admin(user))


def _back_to_menu(lang: str, *, to: str = "menu:home") -> InlineKeyboardMarkup:
    """Every screen but the menu itself carries its way back.

    ``to`` names the *previous* screen — a help page goes back to the help hub,
    everything else goes home. No dead ends, and no deep navigation stack to keep
    track of either.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.back", lang), callback_data=to)
    builder.adjust(1)
    return builder.as_markup()


def _fmt_callback(media_format: str, quality: str) -> str:
    """The callback a choice button carries: ``fmt:<media_format>:<quality>``."""
    return f"{FMT_PREFIX}{media_format}:{quality}"


def _question_keyboard(url: str, lang: str) -> InlineKeyboardMarkup:
    """What can be asked for *this* link — and nothing else.

    Video tiers are one tap (they are already quality words), but audio is two
    steps on purpose: the *file type* first (a .mp3 and a .opus are different
    promises), then its quality presets. A codec with no knob to turn — wav —
    skips straight to the request from its own button. The way back to the menu
    is always the last row, so a user who changed their mind is never stuck in a
    question.
    """
    routing = content.routing_for(url)
    builder = InlineKeyboardBuilder()
    if routing.media_choice is not None:
        builder.button(
            text=t(routing.media_choice.label_key, lang),
            callback_data=_fmt_callback(
                routing.media_choice.media_format, routing.media_choice.quality
            ),
        )
    for codec in routing.audio_formats:
        builder.button(
            text=t(content.AUDIO_FORMAT_LABELS[codec], lang),
            callback_data=(
                f"{AUDF_PREFIX}{codec}"
                if content.audio_level_choices(codec)
                else _fmt_callback("audio", codec)
            ),
        )
    if not routing.audio_formats and routing.media_choice is None:
        # No audio menu and no post to describe — the choices *are* the buttons:
        # the video quality tiers, already quality words in one step.
        for choice in routing.choices:
            builder.button(
                text=t(choice.label_key, lang),
                callback_data=_fmt_callback(choice.media_format, choice.quality),
            )
    builder.adjust(2)
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(2)  # ...the back button then starts a new row of its own
    return builder.as_markup()


def _level_keyboard(codec: str, lang: str) -> InlineKeyboardMarkup:
    """A codec's quality presets, and the way back to the question before it.

    Plain words (best / high / balanced / small size) rather than bitrates: the
    numbers behind them are encoder settings (see services/extractor.py) and a
    menu is no place to make anyone learn them. Back here means *the previous
    screen* — the format question — not Home: nobody should lose their place by
    looking at the next step.
    """
    builder = InlineKeyboardBuilder()
    for choice in content.audio_level_choices(codec):
        builder.button(
            text=t(choice.label_key, lang),
            callback_data=_fmt_callback(choice.media_format, choice.quality),
        )
    builder.adjust(2)
    builder.button(text=t("menu.back", lang), callback_data=f"{AUDF_PREFIX}back")
    builder.adjust(2)
    return builder.as_markup()


def _language_keyboard(
    lang: str, *, back: str | None = "menu:home", mark: bool = True
) -> InlineKeyboardMarkup:
    """The two (or more) languages this bot speaks, each naming itself.

    One picker, three contexts: the first-run choice (``back=None, mark=False`` —
    a new user has no current language to mark and no menu to return to), the
    ``/language`` screen (back to the menu), and the profile's «change language»
    (back to the profile). The buttons are identical everywhere on purpose: one
    handler serves all three, so a language cannot mean two things.
    """
    builder = InlineKeyboardBuilder()
    for code, label in language_options():
        marker = "✅ " if mark and code == normalize_lang(lang) else ""
        builder.button(text=f"{marker}{label}", callback_data=f"{LANG_PREFIX}{code}")
    builder.adjust(len(language_options()))
    if back is not None:
        builder.button(text=t("menu.back", lang), callback_data=back)
        builder.adjust(len(language_options()))
    return builder.as_markup()


def _profile_keyboard(lang: str, *, admin: bool = False) -> InlineKeyboardMarkup:
    """Profile's own actions: language, VIP (not for an admin), then the way back.

    One per row: this is a screen people reach for a specific thing, and
    predictable positions beat density. Premium is the same button the menu used
    to carry — moved, not removed.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.language", lang), callback_data="profile:language")
    if not admin:
        builder.button(text=t("menu.premium", lang), callback_data="menu:premium")
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def _help_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The help hub: its pages, the support contact, and the way home."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("help.btn_how", lang), callback_data="help:how")
    builder.button(text=t("help.btn_audio", lang), callback_data="help:audio")
    builder.button(text=t("help.btn_video", lang), callback_data="help:video")
    builder.button(text=t("help.btn_platforms", lang), callback_data="help:platforms")
    builder.button(text=t("help.btn_problems", lang), callback_data="help:problems")
    builder.button(text=t("menu.support", lang), callback_data="menu:support")
    builder.adjust(2)
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(2)
    return builder.as_markup()


#: Help hub → the catalogue key its page shows. One dict, so a page's button and
#: its text cannot drift apart.
_HELP_PAGES: dict[str, str] = {
    "help:how": "help.body",
    "help:audio": "help.audio",
    "help:video": "help.video",
    "help:platforms": "help.platforms",
    "help:problems": "help.problems",
}


#: A Telegram handle: what a support contact may be written as (``@name`` or bare).
_HANDLE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def support_target(contact: str) -> str:
    """The clickable URL for a configured support contact (``""`` when it is none).

    Three things an operator can put in that field, all of them legitimate: a full
    URL (a web form, a group invite), a ``@username``/bare handle (the usual case),
    and anything else — which is shown as plain text rather than turned into a link
    that goes nowhere.
    """
    text = contact.strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    handle = text.lstrip("@")
    return f"https://t.me/{handle}" if _HANDLE.match(handle) else ""


def _support_line(contact: str, lang: str) -> str:
    """The support screen's body: the configured contact, as a link when it is one."""
    target = support_target(contact)
    shown = escape_html(contact)
    linked = f'<a href="{escape_html(target)}">{shown}</a>' if target else f"<code>{shown}</code>"
    return t("support.text", lang, contact=linked)


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





# ---------------------------------------------------------------------------
# Start / menu
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(
    message: Message,
    user: asyncpg.Record,
    pool: asyncpg.Pool | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    """``/start`` — Home, or the one screen that must come before it.

    A user who has never chosen a language gets the bilingual picker first: their
    language is the one thing not known yet, so asking it *is* the onboarding.
    Everybody else lands on Home directly — the choice is stored on the account
    and never asked again («Change language» is in the Profile).
    """
    if _needs_language_screen(user):
        await message.answer(
            t("language.first_time", lang),
            reply_markup=_language_keyboard(lang, back=None, mark=False),
        )
        return
    await message.answer(
        _welcome_text(user["username"] or t("misc.friend", lang), lang),
        reply_markup=_menu_for(user, lang),
    )


def _needs_language_screen(user: Any) -> bool:
    """Whether ``/start`` opens with the language picker (a genuine first contact).

    Two signals, because ``language`` alone cannot answer this: every row carries
    one from birth — the registration INSERT stores the Telegram locale as a
    *guess* — so a brand-new user is told apart by ``is_new``, the registration
    query's "this call inserted the row". The empty-language arm covers rows that
    predate the column and loose test doubles. Either way the screen shows once:
    a tap stores the choice and the account is never asked again.
    """
    return _is_new(user) or not _speaks(user)


def _is_new(user: Any) -> bool:
    """``True`` only for a record the current update's registration just created."""
    try:
        return bool(user["is_new"])
    except (KeyError, IndexError, TypeError):
        return False


def _speaks(user: Any) -> bool:
    """Whether this account carries a stored language at all.

    ``Record`` raises rather than returning ``None`` for a name it does not carry,
    and test doubles are plain dicts — one guarded read answers both.
    """
    try:
        return bool(user["language"])
    except (KeyError, IndexError, TypeError):
        return False


@router.callback_query(F.data == "menu:home")
async def on_menu_home(
    cb: CallbackQuery,
    user: asyncpg.Record,
    pool: asyncpg.Pool | None = None,
    lang: str = DEFAULT_LANG,
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
        reply_markup=_menu_for(user, lang),
    )


@router.callback_query(F.data == "menu:download")
async def on_menu_download(cb: CallbackQuery, lang: str = DEFAULT_LANG) -> None:
    """Download — the one thing most people came for, said once and clearly."""
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    text = "\n".join((t("download.title", lang), "", t("download.how", lang)))
    await _edit_or_reply(message, text, reply_markup=_back_to_menu(lang))


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
        reply_markup=_profile_keyboard(lang, admin=is_admin(user)),
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
    await _edit_or_reply(message, _help_text(lang), reply_markup=_help_keyboard(lang))


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


@router.callback_query(F.data == "menu:support")
async def on_menu_support(
    cb: CallbackQuery, pool: asyncpg.Pool | None = None, lang: str = DEFAULT_LANG
) -> None:
    """The support button: the contact an operator configured, or honestly nothing.

    Read from the database on every tap rather than baked into the keyboard at
    build time — that is what makes ``/support`` in the admin panel take effect
    without a restart.
    """
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    contact = await database.get_support_contact(pool) if pool is not None else ""
    text = _support_line(contact, lang) if contact else t("support.unset", lang)
    await _edit_or_reply(message, text, reply_markup=_back_to_menu(lang))


@router.message(Command("support"))
async def cmd_support(
    message: Message,
    pool: asyncpg.Pool | None = None,
    user: asyncpg.Record | None = None,
    lang: str = DEFAULT_LANG,
) -> None:
    """``/support`` — the same screen, for someone who types instead of tapping."""
    contact = await database.get_support_contact(pool) if pool is not None else ""
    text = _support_line(contact, lang) if contact else t("support.unset", lang)
    await message.answer(text, reply_markup=_back_to_menu(lang))


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
        t("language.set", chosen, name=lang_button(chosen)),
        reply_markup=_main_menu(chosen, admin=is_admin(user)),
    )


@router.callback_query(F.data == "profile:language")
async def on_profile_language(
    cb: CallbackQuery, state: FSMContext, lang: str = DEFAULT_LANG
) -> None:
    """The profile's «change language»: the same picker, back to the profile.

    The FSM remembers where the picker was opened from, so tapping a language
    hands the *same message* back to the profile in the new language instead of
    dumping the user at Home with no explanation of where they were.
    """
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    await state.update_data(lang_return="profile")
    await _edit_or_reply(
        message,
        t("language.title", lang),
        reply_markup=_language_keyboard(lang, back="menu:profile"),
    )


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
    state: FSMContext | None = None,
    queue: TaskQueue | None = None,
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
    await cb.answer(t("language.set", chosen, name=lang_button(chosen)))
    message = callback_message(cb)
    if message is None:
        return
    # Where the picker was opened from decides where it hands back to: the profile
    # re-renders *itself* in the new language, anything else lands on Home. Either
    # way it is the same message edited — never a chain of new screens.
    origin = ""
    if state is not None:
        data = await state.get_data()
        origin = str(data.pop("lang_return", "") or "")
        await state.set_data(data)
    if origin == "profile":
        await _edit_or_reply(
            message,
            await _profile_text(user, chosen, pool, queue),
            reply_markup=_profile_keyboard(chosen, admin=is_admin(user)),
        )
        return
    await _edit_or_reply(
        message,
        _welcome_text(user["username"] or t("misc.friend", chosen), chosen),
        reply_markup=_menu_for(user, chosen),
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
        await _profile_text(user, lang, pool, queue),
        reply_markup=_profile_keyboard(lang, admin=is_admin(user)),
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
    await message.answer(_help_text(lang), reply_markup=_help_keyboard(lang))


@router.callback_query(F.data.in_(set(_HELP_PAGES)))
async def on_help_page(cb: CallbackQuery, lang: str = DEFAULT_LANG) -> None:
    """A help page: the hub's button becomes that page — back leads to the hub."""
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await cb.answer()
    page = (cb.data or "").strip()
    await _edit_or_reply(
        message,
        _help_page_text(page, lang),
        reply_markup=_back_to_menu(lang, to="menu:help"),
    )


def _help_text(lang: str) -> str:
    """The help hub: one line of invitation, then the pages under it."""
    return "\n".join((t("help.title", lang), "", t("help.intro", lang)))


def _help_page_text(page: str, lang: str) -> str:
    """A page's own text — the catalogue key ``_HELP_PAGES`` maps it to."""
    return "\n".join((t("help.title", lang), "", t(_HELP_PAGES[page], lang)))


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
    user: asyncpg.Record, lang: str, pool: asyncpg.Pool, queue: TaskQueue | None
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
    depth = await queue.depth() if queue is not None else 0
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
            t("profile.language", lang, language=lang_button(lang)),
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
    queue: TaskQueue,
    bot: Bot,
    lang: str = DEFAULT_LANG,
) -> None:
    current = await state.get_state()
    if current == DownloadStates.waiting_format.state:
        # User pasted a new URL while a format choice was pending → replace it,
        # through the same intake path (so a photo post still downloads itself).
        url = extract_url(message.text or "")
        if url and validate_url(url):
            await _queue_url_flow(message, state, user, url, lang, bot=bot, pool=pool, queue=queue)
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
    await _queue_url_flow(message, state, user, url, lang, bot=bot, pool=pool, queue=queue)


@router.message(Command("download"))
async def cmd_download(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    bot: Bot,
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
    await _queue_url_flow(message, state, user, url, lang, bot=bot, pool=pool, queue=queue)


async def _queue_url_flow(
    message: Message,
    state: FSMContext,
    user: asyncpg.Record,
    url: str,
    lang: str,
    *,
    bot: Bot,
    pool: asyncpg.Pool,
    queue: TaskQueue,
) -> None:
    """A link arrives: acknowledge it, check it, then ask — or just start.

    The first thing the user sees is that work has started. The probe below is
    quick, but "quick" and "instant" look very different in a chat window — and the
    message it starts in is the one that ends up carrying the outcome.

    A link with exactly one possible answer never gets asked about. A photo post
    can only be delivered (there is no tier, no audio, nothing to choose), so the
    keyboard would be a single button that cannot be wrong — a menu that looks
    broken. It is queued straight away; ``services/delivery.py`` already knows how
    to send whatever comes back.
    """
    status = await message.answer(t("intake.analyse", lang))
    if not validate_url(url):
        await _edit_or_reply(status, t("intake.invalid_link", lang))
        return
    routing = content.routing_for(url)
    # The router decides whether the *extractor* gets a say. `is_url_supported`
    # answers "does yt-dlp have a site handler for this?"; a link that is itself a
    # file (`pbs.twimg.com/…?format=jpg`, an `i.redd.it` image, a Discord CDN
    # attachment) has none — and does not need one, because it is downloaded
    # directly and the fallback covers the rest. Asking anyway is how a perfectly
    # good photo link ends in "this site is not supported".
    if routing.kind not in _SELF_SERVED_KINDS and not await _probe_supported(url):
        await _edit_or_reply(status, t("intake.unsupported", lang))
        return
    if routing.solo is not None:
        await state.clear()
        await _edit_or_reply(status, t("intake.photo_auto", lang))
        await _submit(
            bot, message, status, pool, queue, user, url, routing.solo, lang, tap=None
        )
        return
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(url=url)
    await _edit_or_reply(
        status, _media_question(url, lang), reply_markup=_question_keyboard(url, lang)
    )


def _media_question(url: str, lang: str) -> str:
    """The question this link deserves — quality, audio format, or its media."""
    return t(content.routing_for(url).header_key, lang)


#: Kinds that are served by the download path itself rather than by a yt-dlp site
#: handler: an image or a gallery is fetched as-is, so yt-dlp's catalogue — which is
#: about *sites* with video — has nothing to say about it.
_SELF_SERVED_KINDS: frozenset[str] = frozenset({"image", "gallery"})


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


@router.callback_query(DownloadStates.waiting_format, F.data.startswith(AUDF_PREFIX))
async def on_audio_format(
    cb: CallbackQuery,
    state: FSMContext,
    lang: str = DEFAULT_LANG,
) -> None:
    """An audio-format tap: the question narrows to that codec's quality.

    Two screens, one state: the link stays in the FSM, and the same message is
    edited from the format grid to the preset grid (and back — ``audf:back`` is
    the level screen's parent, the question itself). A codec without presets
    never reaches here: its button submits the request directly.
    """
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    data = await state.get_data()
    url = data.get("url")
    if not url:
        await cb.answer(t("intake.link_expired", lang), show_alert=True)
        await state.clear()
        return
    spec = (cb.data or "")[len(AUDF_PREFIX) :]
    if spec == "back":
        await cb.answer()
        await _edit_or_reply(
            message,
            _media_question(url, lang),
            reply_markup=_question_keyboard(url, lang),
        )
        return
    # Deliberately strict (same rule as find_choice): the codec must have been a
    # button on *this* link's question — an audio menu has no business opening
    # for a video link, however the tap was spelled.
    if spec not in content.routing_for(url).audio_formats or not content.audio_level_choices(
        spec
    ):
        await cb.answer(t("intake.no_format", lang), show_alert=True)
        return
    await cb.answer()
    await _edit_or_reply(
        message, t("audio.choose_level", lang), reply_markup=_level_keyboard(spec, lang)
    )


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
    # Said before the checks, not after them: the cache lookup, the quota read and
    # the preflight are three round trips, and a tap that produces nothing for a
    # second reads as a broken button.
    status = await message.answer(t("intake.queueing", lang))
    await _submit(bot, message, status, pool, queue, user, url, choice, lang, tap=cb)


async def _submit(
    bot: Bot,
    message: Message,
    status: Message,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    user: asyncpg.Record,
    url: str,
    choice: content.Choice,
    lang: str,
    *,
    tap: CallbackQuery | None,
) -> None:
    """Cache → quota → preflight → queue, with every answer in ``status``.

    Shared by the two ways a download starts: a format button (the user chose) and
    a photo post (there was nothing to choose). One implementation, so the cache,
    the quota and the preflight cannot drift apart between the two — ``tap`` is the
    button that is waiting to be answered, when there is one.
    """
    chat_id = message.chat.id

    # 1) Smart cache hit → resend the previous file_id instantly, no re-download.
    #    The key includes the requested format *and tier*, so an MP3 ask never gets
    #    a video, and a 480p ask never replays the 1080p file.
    cached = await cache_service.get_cached(pool, url, choice.media_format, choice.quality)
    if cached is not None:
        if tap is not None:
            await tap.answer()
        # Same caption as a fresh upload, source link included: the user cannot tell
        # (and should not have to care) whether this file was fetched now or the
        # first time somebody asked for the same link.
        if await send_cached_file(bot, chat_id, cached, caption=replay_caption(cached, lang)):
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
        if tap is not None:
            await tap.answer(t("intake.quota_exhausted_alert", lang), show_alert=True)
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
        if tap is not None:
            await tap.answer()
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
    if tap is not None:
        await tap.answer()
    lines = [
        t("intake.queued", lang, depth=depth),
        t("intake.queued_background", lang),
    ]
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
