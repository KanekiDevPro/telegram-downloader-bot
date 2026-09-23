"""User-facing handlers: start, profile, premium, language, link intake.

The Gateway never downloads anything — it validates the link, checks limits and
the smart cache, then pushes a ``DownloadTask`` into the queue.

Three things about the UI are deliberate. Everything is said in the user's own
language (resolved once by the middleware, carried into the queue so the *worker*'s
messages match). Every screen is *one* message that gets edited in place, so a user
who taps around never collects a stack of menus. And the buttons are drawn from the
link itself (``services/content.py``): a photo post is not offered a 1080p tier, a
YouTube video is not offered "send the photos", and neither is offered an audio
format it cannot produce.

Anything that costs a wait names itself on the card the user is already watching
(a compact ⏳, never a sentence), because a bot that looks frozen is a bot people
tap twice.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Sequence

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
from core.ui import RETRY_PREFIX, callback_message, remember_retry, retry_keyboard, take_retry
from core.ui import edit_or_reply as _edit_or_reply
from core.utils import (
    escape_html,
    extract_url,
    format_size,
    is_video_height,
    today_local,
    validate_url,
)
from handlers.payment import plans_keyboard
from services import cache as cache_service
from services import content, preflight, spotify
from services.delivery import (
    group_add_link,
    media_card,
    quality_label,
    replay_caption,
    send_cached_file,
)
from services.extractor import (
    ExtractorService,
    MediaInfo,
    VideoOption,
    audio_bitrate,
    audio_size_estimate,
)
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
    """HOME — the navigation hub everything hangs off: Download, Profile.

    Deliberate shape, not a flat list. Download gets the top row to itself: it is
    why most people came. Language, the support contact and the store live under
    Profile now — they are account concerns, and a home carrying
    every action is a wall of buttons. And the admin panel button is drawn for
    admins only (an admin is never offered «💎 Go VIP» either: they hold it
    permanently, so the button could only lead to a screen explaining that they
    cannot buy what they already have).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.download", lang), callback_data="menu:download")
    builder.button(text=t("menu.profile", lang), callback_data="menu:profile")
    if admin:
        # The panel used to be reachable only by remembering that `/admin` exists:
        # an operator had no way to tell a missing permission from a missing
        # feature. It is a button now, on the one screen they always open.
        builder.button(text=t("menu.admin", lang), callback_data="menu:admin")
    builder.adjust(1, 2 if admin else 1)
    return builder.as_markup()


def _menu_for(user: asyncpg.Record, lang: str) -> InlineKeyboardMarkup:
    """The home screen as this user should see it (admins get the panel button)."""
    return _main_menu(lang, admin=is_admin(user))


def _back_to_menu(lang: str, *, to: str = "menu:home") -> InlineKeyboardMarkup:
    """Every screen but the menu itself carries its way back.

    ``to`` names the *previous* screen — every screen knows its parent and back
    is that parent, never a hardcoded "home". No dead ends, and no deep
    navigation stack to keep track of either.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.back", lang), callback_data=to)
    builder.adjust(1)
    return builder.as_markup()


def _fmt_callback(media_format: str, quality: str) -> str:
    """The callback a choice button carries: ``fmt:<media_format>:<quality>``."""
    return f"{FMT_PREFIX}{media_format}:{quality}"


#: How long a repeat tap is treated as the *same* tap. Telegram delivers a
#: second callback while the first is still being handled (a double-tap, an
#: impatient re-tap on the same keyboard), and the queue would happily run both.
_DOUBLE_TAP_WINDOW_S = 5.0

#: Per user: ``(when, which request)`` last accepted for the queue.
_recent_requests: dict[int, tuple[float, str]] = {}


def _double_tap(user_id: int, request: str, *, now: float | None = None) -> bool:
    """``True`` when this is the same tap arriving twice — acknowledge and drop.

    Server-side on purpose: removing the keyboard is UI, and the UI cannot promise
    "one tap, one download" against a client that sends callbacks faster than
    edits land. The first tap's timestamp is kept, so a third and fourth tap are
    swallowed for the same window.
    """
    moment = time.monotonic() if now is None else now
    seen = _recent_requests.get(user_id)
    if seen is not None and seen[1] == request and moment - seen[0] < _DOUBLE_TAP_WINDOW_S:
        return True
    _recent_requests[user_id] = (moment, request)
    if len(_recent_requests) > 4096:  # opportunistic pruning of silent users
        for uid, (at, _) in list(_recent_requests.items()):
            if moment - at >= _DOUBLE_TAP_WINDOW_S:
                _recent_requests.pop(uid, None)
    return False


def _sized_quality_rows(
    options: Sequence[VideoOption], lang: str
) -> list[tuple[str, str]]:
    """One button per *real* resolution: quality first, size second.

    The rows come from what the link actually has (``MediaInfo.video_options``),
    best first — never a raw extractor list, never an invented tier. The quality
    is named the conventional way (1920x1080 is 1080p — never a pixel width); a
    size the site only estimated wears its ``~``; a size it never reported is
    *omitted*, because a wrong number is worse than no number. The markers rank:
    ⭐ the recommended top, 🔥 the popular runner-up, 🎬 the rest — emoji that
    communicate hierarchy, not decoration.
    """
    rows: list[tuple[str, str]] = []
    ordered = sorted(options, key=lambda option: option.label_p, reverse=True)
    for position, option in enumerate(ordered):
        label = t("media.quality_p", lang, height=option.label_p)
        if option.size_bytes:
            size = _compact_size(option.size_bytes)
            if size:
                label = f"{label} · {'' if option.size_exact else '~'}{size}"
        marker = ("⭐", "🔥", "🎬")[min(position, 2)]
        rows.append((f"{marker} {label}", _fmt_callback("video", str(option.height))))
    return rows


def _compact_size(num_bytes: int) -> str:
    """Button-sized: "14 MB" rather than "14.0 MB" — the card keeps its decimals."""
    return format_size(num_bytes, unknown="").replace(".0 ", " ")


def _question_keyboard(
    url: str, lang: str, *, options: Sequence[VideoOption] = ()
) -> InlineKeyboardMarkup:
    """What can be asked for *this* link — and nothing else.

    Video qualities are one tap (they are already quality words), but audio is
    two steps on purpose: the *file type* first (a .mp3 and a .opus are different
    promises), then its quality presets. A codec with no knob to turn — wav —
    skips straight to the request from its own button. The way back to the menu
    is always the last row, so a user who changed their mind is never stuck in a
    question.
    """
    routing = content.routing_for(url)
    builder = InlineKeyboardBuilder()
    row_width = 2
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
        # No audio menu and no post to describe — the choices *are* the buttons.
        # With probed metadata they are the link's own resolutions (one row each:
        # the label is a headline, the size is secondary); without it, the tier
        # menu whose labels still say "up to".
        if options:
            sized = _sized_quality_rows(options, lang)
            for label, data in sized:
                builder.button(text=label, callback_data=data)
            # One per row when the ladder is long (mobile width fits the label);
            # a short list shares rows so the screen is not three lonely strips.
            row_width = 2 if len(sized) <= 2 else 1
        else:
            for choice in routing.choices:
                builder.button(
                    text=t(choice.label_key, lang),
                    callback_data=_fmt_callback(choice.media_format, choice.quality),
                )
    # The way out leads to the Download screen — this question is its child (and
    # the retry's ⬅️ lands there too), never to an unrelated screen.
    builder.button(text=t("menu.back", lang), callback_data="menu:download")
    builder.adjust(row_width)  # the back button starts a row of its own
    return builder.as_markup()


#: The rows' hierarchy marks, bottom-up: the last row is 📦, above it ⚖️ then 🔥,
#: and whatever tops the list is 💎. The marks rank the rows *shown* — a
#: source-capped ladder still leads with 💎: the best this link can honestly use.
_MARKS_UP: tuple[str, ...] = ("📦", "⚖️", "🔥")


def _marks(count: int) -> tuple[str, ...]:
    if count <= 1:
        return ("💎",)
    rows = ["⚖️"] * count
    rows[0] = "💎"
    rows[-1] = "📦"
    for step, mark in enumerate(_MARKS_UP[1:], start=1):
        position = count - 1 - step
        if position > 0:
            rows[position] = mark
    return tuple(rows)


def _level_rows(
    codec: str, lang: str, *, duration: float = 0, source_kbps: int | None = None
) -> list[tuple[str, str]]:
    """``[(button label, callback), ...]`` for a codec's presets, best first.

    The labels are the real thing: the kbps this tier encodes at, and — when the
    link's length is known — what the file will weigh (rate × length, so a ``~``
    estimate). When the source's own rate is known, the re-encodes *above* it are
    hidden: a bigger number on a smaller source is only a bigger file, and a menu
    that offers it is a menu that lies. The untouched source stream is never
    hidden (it *is* the source), and a ladder that would empty out keeps its floor
    — the least misleading row left. Raw or lossless output never reaches here at
    all (no presets, no screen).
    """
    def honest(level: str, tier: str) -> bool:
        kbps = audio_bitrate(tier)
        return source_kbps is None or kbps is None or kbps <= source_kbps

    levels = content.audio_tier_levels(codec)
    kept = [(level, tier) for level, tier in levels if honest(level, tier)]
    if not kept and levels:
        kept = [levels[-1]]
    rows: list[tuple[str, str]] = []
    for mark, (_level, tier) in zip(_marks(len(kept)), kept):
        kbps = audio_bitrate(tier)
        if kbps:
            label = f"{mark} {kbps} kbps"
        else:
            label = f"{mark} {t('audio.original_long', lang)}"
        estimate = audio_size_estimate(tier, duration)
        if estimate:
            label += f" · ~{format_size(estimate, unknown='')}"
        rows.append((label, _fmt_callback("audio", str(tier))))
    return rows


def _source_kbps_of(data: dict[str, Any]) -> int | None:
    """The source audio rate the probe found (``None`` when it found nothing)."""
    raw = data.get("source_kbps")
    return int(raw) if isinstance(raw, (int, float)) and raw > 0 else None


def _source_rate_note(codec: str, data: dict[str, Any], lang: str) -> str:
    """The ℹ️ footnote — only when the ladder was actually trimmed.

    A note that restates what the rows already show is clutter; this one exists
    only to explain what is *missing* (the bigger re-encodes of a smaller source)
    — and to say so honestly: a measured rate gets a ``~``, a declared one does
    not.
    """
    source_kbps = _source_kbps_of(data)
    if source_kbps is None:
        return ""
    if not any(
        (audio_bitrate(tier) or 0) > source_kbps
        for _level, tier in content.audio_tier_levels(codec)
    ):
        return ""
    rate = f"{'~' if data.get('source_kbps_approx') else ''}{source_kbps} kbps"
    return t("audio.source_rate", lang, rate=rate)


def _level_keyboard(
    codec: str, lang: str, *, duration: float = 0, source_kbps: int | None = None
) -> InlineKeyboardMarkup:
    """A codec's quality presets, and the way back to the question before it.

    Bitrates, not moods: the number is what the file will be — and per format,
    so the MP3 screen says 320/256/192/128 while OPUS says 192/128/96/64. Back
    here means *the previous screen* — the format question — not Home: nobody
    should lose their place by looking at the next step.
    """
    builder = InlineKeyboardBuilder()
    for label, data in _level_rows(
        codec, lang, duration=duration, source_kbps=source_kbps
    ):
        builder.button(text=label, callback_data=data)
    builder.adjust(1)
    builder.button(text=t("menu.back", lang), callback_data=f"{AUDF_PREFIX}back")
    builder.adjust(1)
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
    """Profile's own actions: language, VIP (not for an admin), support, back.

    One per row: this is a screen people reach for a specific thing, and
    predictable positions beat density. Premium is the same button the menu used
    to carry — moved, not removed — and the support contact moved here too when
    the help section went away: it is who to ask, which is what a profile is for.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("menu.language", lang), callback_data="profile:language")
    if not admin:
        builder.button(text=t("menu.premium", lang), callback_data="menu:premium")
    builder.button(text=t("menu.support", lang), callback_data="menu:support")
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


def _download_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The download screen's own controls: the group flow, and the way back.

    «Add to a group» is Telegram's own picker (a ``?startgroup`` deep link to the
    bot's real address) — the button exists only once the bot knows its own
    @handle, because a wrong link is worse than no button.
    """
    builder = InlineKeyboardBuilder()
    link = group_add_link()
    if link:
        builder.button(text=t("menu.add_group", lang), url=link)
    builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


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
    await _edit_or_reply(message, text, reply_markup=_download_keyboard(lang))


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
    if message.chat.type != "private" and not extract_url(message.text or ""):
        # A group is link-driven: no link in the message, no message from the bot
        # — group chats stay clean. Commands and links still work as in private.
        return
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
    """A link arrives: check it, then ask — or just start.

    No acknowledgement message. The *question* is the first thing the user sees —
    one message carrying the media card and the choice — and it is the message
    the outcome lands in. "Analysing…" lines and "added to the queue" receipts are
    all gone on purpose: the tap, the wait and the file tell the story.

    A link with exactly one possible answer never gets asked about. A photo post
    can only be delivered (there is no tier, no audio, nothing to choose), so the
    keyboard would be a single button that cannot be wrong — a menu that looks
    broken. It is queued straight away; ``services/delivery.py`` already knows how
    to send whatever comes back.
    """
    if not validate_url(url):
        await message.answer(t("intake.invalid_link", lang))
        return
    routing = content.routing_for(url)
    # The router decides whether the *extractor* gets a say. `is_url_supported`
    # answers "does yt-dlp have a site handler for this?"; a link that is itself a
    # file (`pbs.twimg.com/…?format=jpg`, an `i.redd.it` image, a Discord CDN
    # attachment) has none — and does not need one, because it is downloaded
    # directly and the fallback covers the rest. Asking anyway is how a perfectly
    # good photo link ends in "this site is not supported".
    if routing.kind not in _SELF_SERVED_KINDS and not await _probe_supported(url):
        await message.answer(t("intake.unsupported", lang))
        return
    if routing.solo is not None:
        await state.clear()
        status = await message.answer(media_card(url=url, lang=lang))
        await _submit(
            bot,
            status,
            pool,
            queue,
            user,
            url,
            routing.solo.media_format,
            routing.solo.quality,
            lang,
            tap=None,
        )
        return
    # The question's header is the media card: 🎬 and 🔗 answer "what is this and
    # where is it from" before asking "what should I do with it". The probe that
    # fills the title also discovers the resolutions that really exist (and their
    # sizes), so the menu is drawn from the link itself. A probe that gives
    # nothing costs only the polish: the tier menu and a card that shows its link.
    await _typing(bot, message.chat.id)
    info = await _probe_meta(bot, url)
    title = _clean_title(info, url)
    options = tuple(info.video_options) if info is not None else ()
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(
        url=url,
        title=title,
        duration=(float(info.duration or 0) if info is not None else 0),
        source_kbps=(info.audio_kbps if info is not None else None),
        source_kbps_approx=(info.audio_kbps_approx if info is not None else False),
        offered=_offered_tiers(url, options),
        options=[
            (option.height, option.label_p, option.size_bytes, option.size_exact)
            for option in options
        ],
    )
    await message.answer(
        _question_text(url, lang, title=title),
        reply_markup=_question_keyboard(url, lang, options=options),
    )


def _question_text(
    url: str, lang: str, *, title: str = "", prompt: str | None = None
) -> str:
    """The card, then the one line asking — never two messages.

    The card's 🎞 line is *deliberately* absent here: nothing has been chosen yet.
    It appears the moment a choice is made, naming it.
    """
    card = media_card(title=title, url=url, lang=lang)
    text = prompt if prompt is not None else t(content.routing_for(url).header_key, lang)
    return f"{card}\n\n{text}" if card else text


def _clean_title(info: MediaInfo | None, url: str) -> str:
    """A title worth its 🎬 line — or ``""`` so the line is simply not there."""
    title = (info.title if info is not None else "").strip()
    return "" if not title or title == url.strip() else title


def _offered_tiers(url: str, options: Sequence[VideoOption]) -> list[str]:
    """Exactly what the question's buttons say a tap may choose — the vocabulary
    the tap is later validated against (a crafted callback is data)."""
    if options:
        return [str(option.height) for option in options]
    return [choice.quality for choice in content.routing_for(url).choices]


async def _typing(bot: Bot, chat_id: int) -> None:
    """Say "working" without a word — Telegram's typing state, best-effort.

    A nicety, so it never raises: a bot double without the method (or a chat that
    blocks the bot) must not cost the flow anything.
    """
    try:
        await bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass


#: Kinds that are served by the download path itself rather than by a yt-dlp site
#: handler: an image or a gallery is fetched as-is, so yt-dlp's catalogue — which is
#: about *sites* with video — has nothing to say about it.
_SELF_SERVED_KINDS: frozenset[str] = frozenset({"image", "gallery"})


#: How long a metadata probe may hold the question open. Long enough for a slow
#: site, short enough that a hung one costs a fallback menu, not the UX.
_META_PROBE_TIMEOUT_S = 5.0


async def _probe_meta(bot: Bot, url: str) -> MediaInfo | None:
    """What the question needs to know: the title, and which resolutions exist.

    Answered by the extraction pipeline's own metadata (``bot.state.extractor``) —
    never a second engine, never a network call of this module's own. Anything
    goes wrong (a blocked host, a slow one, a test double with no extractor at
    all) and the answer is simply ``None``: the question is still asked, with the
    tier menu and a card that shows its link.
    """
    extractor = getattr(getattr(bot, "state", None), "extractor", None)
    if extractor is None:
        return None
    try:
        return await asyncio.wait_for(
            extractor.extract(url), timeout=_META_PROBE_TIMEOUT_S
        )
    except Exception:
        logger.info("metadata probe gave nothing for %.80s — falling back to tiers", url)
        return None


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
    title = str(data.get("title") or "")
    spec = (cb.data or "")[len(AUDF_PREFIX) :]
    if spec == "back":
        await cb.answer()
        await _edit_or_reply(
            message,
            _question_text(url, lang, title=title),
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
    prompt = t("audio.choose_level_fmt", lang, format=spec.upper())
    note = _source_rate_note(spec, data, lang)
    if note:
        prompt = f"{prompt}\n\n{note}"
    await _edit_or_reply(
        message,
        _question_text(url, lang, title=title, prompt=prompt),
        reply_markup=_level_keyboard(
            spec,
            lang,
            duration=float(data.get("duration") or 0),
            source_kbps=_source_kbps_of(data),
        ),
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
    """A final choice: acknowledge it silently and let the card answer."""
    data = await state.get_data()
    url = data.get("url")
    if not url:
        await cb.answer(t("intake.link_expired", lang), show_alert=True)
        await state.clear()
        return
    media_format, quality = _parse_format(cb.data)
    if not _tap_was_offered(url, media_format, quality, data):
        # A tap that was never offered (an older menu, a forwarded message, a
        # crafted callback): nothing is queued, and the tap is answered honestly.
        await cb.answer(t("intake.no_format", lang), show_alert=True)
        return

    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await state.clear()
    await _submit(
        bot,
        message,
        pool,
        queue,
        user,
        url,
        media_format,
        quality,
        lang,
        tap=cb,
        title=str(data.get("title") or ""),
        size_hint=_size_hint(data, quality, media_format),
    )


def _size_hint(
    data: dict[str, Any], quality: str, media_format: str = "video"
) -> tuple[int, bool] | None:
    """The chosen option's expected size (bytes, exact?) — or ``None``.

    What the probed menu showed is what the card repeats after the tap: the size
    the site reported, marked estimated when it was. An audio tier is never
    *reported* — its figure is rate × the link's own length (arithmetic, so
    always estimated and marked so). Nothing to go on → quality alone.
    """
    for height, _label, size_bytes, exact in data.get("options") or []:
        if str(height) == str(quality) and size_bytes:
            return (int(size_bytes), bool(exact))
    if media_format == "audio":
        estimate = audio_size_estimate(quality, float(data.get("duration") or 0))
        if estimate:
            return (estimate, False)
    return None


def _tap_was_offered(
    url: str, media_format: str, quality: str, data: dict[str, Any]
) -> bool:
    """Whether this tap was a button on the screen that is up.

    Two vocabularies, one rule. The router's static menu answers through
    ``find_choice`` (deliberately strict — see its own note). The *probed* video
    rows are a vocabulary no table knows, so what the question showed travels in
    the FSM instead: the offered list decides, and a crafted height is still just
    data (``is_video_height`` agrees). When a menu predates the offered list, the
    static rule is all there is.
    """
    if not media_format or not quality:
        return False
    if media_format == "video" and is_video_height(quality) and data.get("offered") is not None:
        return quality in [str(item) for item in data["offered"]]
    return content.find_choice(url, media_format, quality) is not None


@router.callback_query(F.data.startswith(RETRY_PREFIX))
async def on_retry(
    cb: CallbackQuery,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    bot: Bot,
    lang: str = DEFAULT_LANG,
) -> None:
    """One failed download, tried again — the same job, a fresh run.

    What to re-run lives behind the button's key *server-side* (``core.ui``), and
    only the user whose job it was may press it: a crafted key can name somebody
    else's failure at most, and it comes back empty-handed. The retry re-enters
    the ordinary path — cache, quota and preflight all get their say again.
    """
    payload = take_retry((cb.data or "")[len(RETRY_PREFIX) :], owner=user["telegram_id"])
    if payload is None:
        await cb.answer(t("intake.link_expired", lang), show_alert=True)
        return
    task = DownloadTask.from_payload(payload["task"])
    if task.telegram_id != user["telegram_id"]:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("intake.stale", lang), show_alert=True)
        return
    await _submit(
        bot,
        message,
        pool,
        queue,
        user,
        task.url,
        task.media_format,
        task.quality,
        lang,
        tap=cb,
        title=task.title,
    )


@router.callback_query(F.data.startswith((FMT_PREFIX, AUDF_PREFIX)))
async def on_stale_media_tap(cb: CallbackQuery, user: asyncpg.Record, lang: str = DEFAULT_LANG) -> None:
    """A tap on a screen that is no longer up: answer it, change nothing.

    The state-scoped handlers above own every *live* menu; this is what a second
    tap on a consumed keyboard (or an old, forwarded one) lands on — and an
    unanswered callback spins forever, which is worse than any answer. A repeat
    of the request that *just* left for the queue is swallowed quietly (one tap,
    one download); everything else gets the honest "no longer live".
    """
    data = cb.data or ""
    media_format, quality = _parse_format(data)
    request = f"{media_format}:{quality}" if media_format else data
    if _double_tap(user["telegram_id"], request):
        await cb.answer()
        return
    await cb.answer(t("intake.stale", lang), show_alert=True)


async def _submit(
    bot: Bot,
    message: Message,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    user: asyncpg.Record,
    url: str,
    media_format: str,
    quality: str,
    lang: str,
    *,
    tap: CallbackQuery | None,
    title: str = "",
    size_hint: tuple[int, bool] | None = None,
) -> None:
    """Tap → wait → the file: cache → quota → preflight → queue, on one card.

    ``message`` is the card message, and it stays the job's whole story: the
    keyboard goes the moment the choice is made (a screen that answers is a
    screen with no buttons left), the wait is silent — the ⏳ state, never a
    sentence — and the media that arrives is the confirmation. Shared by every
    way a download starts (a button, a photo post, a retry), so the cache, the
    quota and the preflight cannot drift apart between them. ``tap`` is the
    button waiting to be acknowledged, when there is one.
    """
    chat_id = message.chat.id
    request = f"{media_format}:{quality}"

    # 0) The same tap twice is one download — acknowledged quietly and dropped.
    #    The queue must never see the second copy; the UI alone cannot promise it.
    if _double_tap(user["telegram_id"], request):
        if tap is not None:
            await tap.answer()
        return

    # 1) The choice is final: the card names it, the keyboard goes away.
    size = ""
    if size_hint is not None:
        size = f"{'' if size_hint[1] else '~'}{format_size(size_hint[0], unknown='')}"
    card = media_card(
        title=title,
        url=url,
        quality=quality_label(media_format, quality, lang),
        size=size,
        audio=media_format == "audio",
        lang=lang,
    )
    await _edit_or_reply(
        message, card, reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
    )
    if tap is not None:
        await tap.answer()  # silent: the screen change *is* the acknowledgement

    task = DownloadTask(
        url=url,
        telegram_id=user["telegram_id"],
        chat_id=chat_id,
        media_format=media_format,  # type: ignore[arg-type]
        quality=quality,
        lang=lang,
        url_hash=cache_service.cache_key(url, media_format, quality),
        title=title,
        status_message_id=message.message_id,
        chat_title=(getattr(message.chat, "title", "") or ""),
    )

    # 2) Smart cache hit → resend the previous file_id instantly, no re-download.
    #    The key includes the requested format *and tier*, so an MP3 ask never gets
    #    a video, and a 480p ask never replays the 1080p file. The replay carries
    #    the same media card, so the user cannot tell (and should not have to care)
    #    whether this file was fetched now or the first time somebody asked.
    cached = await cache_service.get_cached(pool, url, media_format, quality)
    if cached is not None:
        if await send_cached_file(bot, chat_id, cached, caption=replay_caption(cached, lang)):
            return
        # dead file_id → drop it and fall through to a real download
        await cache_service.forget(pool, url, media_format, quality)

    # 3) Soft daily-quota check (the worker claims the slot atomically).
    usage = await database.get_daily_usage(pool, user["telegram_id"])
    used = usage["daily_downloads"] if usage and usage["last_download_date"] == today_local() else 0
    limit = effective_daily_limit(user)
    if used >= limit:
        await _edit_or_reply(
            message,
            f"{card}\n\n{t('intake.quota_exhausted', lang, used=used, limit=limit)}",
            reply_markup=_back_to_menu(lang, to="menu:download"),
        )
        return

    # 4) Preflight: a YouTube link that cannot work should not cost a wait. Only
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
        # A refusal with one way forward: try again (after the fix), or leave.
        key = remember_retry({"task": task.to_payload()}, owner=user["telegram_id"])
        await _edit_or_reply(
            message,
            f"{card}\n\n{verdict.message}",
            reply_markup=retry_keyboard(key, lang),
        )
        return

    # 5) Push to the queue — and say nothing: the card already did. The file
    #    arrives as its own message; warnings and receipts are the chat's noise.
    await queue.enqueue(task)


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
