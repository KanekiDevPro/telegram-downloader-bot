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
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Sequence

import aiohttp
import asyncpg
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message
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
    default_quality,
    escape_html,
    extract_url,
    format_size,
    today_local,
    validate_url,
)
from handlers.payment import plans_keyboard
from services import cache as cache_service
from services import content, preflight, spotify
from services.delivery import (
    ActionPulse,
    format_duration,
    group_add_link,
    label_for_request,
    media_card,
    quality_label,
    replay_caption,
    resolution_name,
    send_cached_file,
)
from services.extractor import (
    AudioCapability,
    ExtractorService,
    MediaInfo,
    VideoOption,
    audio_bitrate,
    audio_capability,
    audio_is_original,
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
#: The one retry on a question whose qualities could not be discovered: it
#: re-runs the metadata probe (a stale screen is re-extracted, never answered
#: with a default download). Constant, not a payload — the link it re-probes is
#: server-side state (the FSM), so a crafted callback can at most name it.
PROBE_CALLBACK = "probe:retry"
LANG_PREFIX = "lang:"

#: No link previews under intake text: the card already names the link, and a
#: preview would grow a second, uncontrolled page under a question.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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


#: Extractions this gateway is running right now, per ``(user, link)``. The
#: probe's retry button is not single-use, and a lookup that is still running
#: *is* the retry: a spammed button must never stack parallel extractions of the
#: same link. Released the moment the lookup answers, so a later tap re-extracts
#: for real.
_probes_running: set[tuple[int, str]] = set()


def _sized_quality_rows(
    options: Sequence[VideoOption], lang: str
) -> list[tuple[str, str]]:
    """One button per *real* resolution: quality first, size second.

    The rows come from what the link actually has (``MediaInfo.video_options``),
    best first — never a raw extractor list, never an invented tier, no ranking
    labels over the top of them. The quality is named the conventional way
    (1920x1080 is 1080p — never a pixel width; the big ones get their 2K/4K);
    a size the site only estimated wears its ``~``; a size it never reported
    *says so* — the resolution is real and stays on the row, because a missing
    row hides the truth harder than a missing number ever could.
    """
    rows: list[tuple[str, str]] = []
    ordered = sorted(options, key=lambda option: option.label_p, reverse=True)
    for option in ordered:
        label = resolution_name(option.label_p, lang)
        if option.size_bytes:
            size = _compact_size(option.size_bytes)
            if size:
                label = f"{label} · {'' if option.size_exact else '~'}{size}"
        else:
            label = f"{label} · {t('media.size_unknown', lang)}"
        rows.append((label, _fmt_callback("video", str(option.height))))
    return rows


def _producible_formats(codecs: tuple[str, ...]) -> tuple[str, ...]:
    """Only the formats this engine can actually finish.

    Every re-encode needs ffmpeg; without it the untouched source stream (m4a)
    is the one promise the download path can keep. A FLAC button that could only
    end in failure is worse than no FLAC button.
    """
    if not codecs or shutil.which("ffmpeg"):
        return codecs
    return ("m4a",) if "m4a" in codecs else ()


def _compact_size(num_bytes: int) -> str:
    """Button-sized: "14 MB" rather than "14.0 MB" — the card keeps its decimals."""
    return format_size(num_bytes, unknown="").replace(".0 ", " ")


def _question_keyboard(
    url: str,
    lang: str,
    *,
    options: Sequence[VideoOption] = (),
    capability: AudioCapability | None = None,
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
    offered_audio = _producible_formats(routing.audio_formats)
    if capability is not None:
        # What this source can really become beats the static catalogue — the
        # capability model decides the buttons (services.extractor.audio_capability).
        offered_audio = tuple(codec for codec in offered_audio if codec in capability.formats)
    for codec in offered_audio:
        builder.button(
            text=t(content.AUDIO_FORMAT_LABELS[codec], lang),
            callback_data=(
                f"{AUDF_PREFIX}{codec}"
                if content.audio_level_choices(codec)
                else _fmt_callback("audio", codec)
            ),
        )
    # The link's own resolutions, one row each, whenever the probe really found
    # them — a post link included, whose route would otherwise bury a valid
    # format list before the screen is ever drawn. An audio link keeps its format
    # grid as the only question (its streams have no resolutions to show).
    #
    # A video-capable link with *no* discovered ladder never reaches this
    # function: the intake flow answers it with an explicit error and a retry
    # (see ``_ask_again``) — a generic «download it» row here would silently
    # substitute the default format for a capability lookup that failed.
    show_rows = bool(options) and (not routing.audio_formats or routing.media_choice is not None)
    if show_rows:
        sized = _sized_quality_rows(options, lang)
        for label, data in sized:
            builder.button(text=label, callback_data=data)
        # One per row when the ladder is long (mobile width fits the label);
        # a short list shares rows so the screen is not three lonely strips.
        row_width = 2 if len(sized) <= 2 else 1
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
    codec: str,
    lang: str,
    *,
    duration: float = 0,
    source_kbps: int | None = None,
    copy_ok: bool = True,
    upload_limit_bytes: int = 0,
) -> list[tuple[str, str]]:
    """``[(button label, callback), ...]`` for a codec's presets, best first.

    The labels are the real thing: the kbps this tier encodes at, and — when the
    link's length is known — what the file will weigh (rate × length, so a ``~``
    estimate). When the source's own rate is known, the re-encodes *above* it are
    hidden: a bigger number on a smaller source is only a bigger file, and a menu
    that offers it is a menu that lies. The untouched source stream is kept (it
    *is* the source) unless it would arrive in a different container than its
    button names, and a row the transport cannot carry is gone too — an option
    that only ends in a refusal is not an option. A ladder that would empty out
    keeps its floor — the least misleading row left. Raw or lossless output never
    reaches here at all (no presets, no screen).
    """
    def honest(level: str, tier: str) -> bool:
        if audio_is_original(tier) and not copy_ok:
            return False
        kbps = audio_bitrate(tier)
        if kbps and source_kbps is not None and kbps > source_kbps:
            return False
        estimate = audio_size_estimate(tier, duration)
        return not (estimate and upload_limit_bytes and estimate > upload_limit_bytes)

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
    """The source's own rate, said once — so the rows can be judged against it.

    Shown whenever the source declared (or measured) one, trimmed ladder or not:
    "what I have" is the fact that makes every option below it honest. A measured
    rate gets its ``~``; a declared one does not.
    """
    source_kbps = _source_kbps_of(data)
    if source_kbps is None:
        return ""
    rate = f"{'~' if data.get('source_kbps_approx') else ''}{source_kbps} kbps"
    return t("audio.source_rate", lang, rate=rate)


def _stored_capability(data: dict[str, Any]) -> AudioCapability | None:
    """The question's audio capability as it was shown (``None`` for old menus).

    What the buttons offered travels in the FSM — a crafted callback is data, and
    the list it is checked against must be the list the user actually saw.
    """
    offered = data.get("audio_offered")
    if offered is None:
        return None
    return AudioCapability(
        formats=tuple(str(item) for item in offered),
        copy_ok=data.get("copy_ok") is not False,
    )


def _level_keyboard(
    codec: str,
    lang: str,
    *,
    duration: float = 0,
    source_kbps: int | None = None,
    copy_ok: bool = True,
    upload_limit_bytes: int = 0,
) -> InlineKeyboardMarkup:
    """A codec's quality presets, and the way back to the question before it.

    Bitrates, not moods: the number is what the file will be — and per format,
    so the MP3 screen says 320/256/192/128 while OPUS says 192/128/96/64. Back
    here means *the previous screen* — the format question — not Home: nobody
    should lose their place by looking at the next step.
    """
    builder = InlineKeyboardBuilder()
    for label, data in _level_rows(
        codec,
        lang,
        duration=duration,
        source_kbps=source_kbps,
        copy_ok=copy_ok,
        upload_limit_bytes=upload_limit_bytes,
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
            await message.answer(
                t("intake.invalid_link", lang), link_preview_options=_NO_PREVIEW
            )
        return
    if current is not None:
        await message.answer(t("intake.step_in_progress", lang), link_preview_options=_NO_PREVIEW)
        return

    url = extract_url(message.text or "")
    if not url:
        await message.answer(t("intake.no_link_found", lang), link_preview_options=_NO_PREVIEW)
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
        await message.answer(t("intake.download_usage", lang), link_preview_options=_NO_PREVIEW)
        return
    await _queue_url_flow(message, state, user, url, lang, bot=bot, pool=pool, queue=queue)


#: The delete tasks in the air. A fire-and-forget task nothing references can be
#: garbage-collected mid-flight (the loop keeps only a weak grip on it), so this
#: set is the grip — and the one place a test can wait for the deletion.
_pending_deletes: set[asyncio.Task[None]] = set()


def _forget_raw_link(message: Message) -> None:
    """Take the raw link out of the chat *now* — and make nothing wait for it.

    The old order — delete once the bot had answered — made the courtesy cost
    seconds: the menu is only drawn after the metadata probe, so the pasted URL
    (and the preview some unofficial clients draw under it) sat in the chat for
    the whole extraction. The link is known-good before any of that starts, so
    the delete leaves immediately and runs *alongside* the flow.
    """
    task = asyncio.create_task(_delete_raw_link(message))
    _pending_deletes.add(task)
    task.add_done_callback(_pending_deletes.discard)


async def _delete_raw_link(message: Message) -> None:
    """The deletion itself — silent in every outcome.

    Deleting is a courtesy, never a requirement: a bot without delete rights
    (a group where it is not an admin), a message already gone, or a network
    hiccup must all leave the message where it is — and must never surface in a
    handler or crash the fire-and-forget task above.
    """
    try:
        await message.delete()
    except Exception:
        pass


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

    A real link is answered whatever happens next, so its raw copy leaves right
    here — fired off before the work below and running alongside it
    (``_forget_raw_link``). An invalid link is not "answered" into deletion: what
    the user wrote — no link, or a link-shaped typo — stays where it is.
    """
    if not validate_url(url):
        await message.answer(t("intake.invalid_link", lang), link_preview_options=_NO_PREVIEW)
        return
    _forget_raw_link(message)
    # Share/short links and media *viewer* wrappers name their content somewhere
    # else: resolve them once, here — routing, the engines and the direct-download
    # path all see the real thing afterwards (see _canonical_url).
    url = await _canonical_url(url)
    routing = content.routing_for(url)
    # The router decides whether the *extractor* gets a say. `is_url_supported`
    # answers "does yt-dlp have a site handler for this?"; a link that is itself a
    # file (`pbs.twimg.com/…?format=jpg`, an `i.redd.it` image, a Discord CDN
    # attachment) has none — and does not need one, because it is downloaded
    # directly and the fallback covers the rest. Asking anyway is how a perfectly
    # good photo link ends in "this site is not supported".
    if routing.kind not in _SELF_SERVED_KINDS and not await _probe_supported(url):
        await message.answer(t("intake.unsupported", lang), link_preview_options=_NO_PREVIEW)
        # A *valid* link, answered — even though the answer is «no». Its raw
        # copy left the chat the moment it was recognized.
        return
    if routing.solo is not None:
        await state.clear()
        status = await message.answer(
            media_card(url=url, lang=lang), link_preview_options=_NO_PREVIEW
        )
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
            # The auto-best path never asked for a tier, so anything this URL has
            # already produced answers it — the zero-wait rule for solo links.
            fallback_any_tier=True,
        )
        return
    await _ask_about_link(
        message, state, user, url, lang, bot=bot, pool=pool, queue=queue
    )


async def _ask_about_link(
    message: Message,
    state: FSMContext,
    user: asyncpg.Record,
    url: str,
    lang: str,
    *,
    bot: Bot,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    edit: bool = False,
    force_probe: bool = False,
) -> None:
    """Probe what this link really has, then ask — or say why there is no menu.

    The question's header is the media card: 🎬 and 🔗 answer "what is this and
    where is it from" before asking "what should I do with it". The probe that
    fills the title also discovers the resolutions that really exist (and their
    sizes), so the menu is drawn from the link itself. When a *video-capable*
    link's qualities cannot be discovered, the answer is an explicit error with a
    retry that re-extracts — never a silent substitution of the default format
    for a capability lookup that failed. ``edit`` rewrites the message being
    watched (the retry case) instead of opening a new one.

    A link the cache already knows skips the probe entirely (``_ask_from_cache``):
    its stored requests *are* a menu, and their taps replay instantly. ``force_probe``
    is the deliberate re-check — the full ladder wants extraction, and says so.
    """
    routing = content.routing_for(url)
    if not force_probe:
        # The zero-wait path: what this link has already produced is a menu whose
        # every tap replays at once (the tap's own cache check) — so the extractor
        # never runs and the question is drawn from the rows instead.
        rows = await _cached_rows(pool, url)
        if rows:
            await _ask_from_cache(message, state, url, lang, rows, edit=edit)
            return
    info: MediaInfo | None = None
    duration = 0.0
    prompt: str | None = None
    if spotify.is_spotify_url(url):
        # yt-dlp refuses Spotify by policy, so there is nothing to probe — the
        # link is served by mapping it to its public YouTube counterpart, and
        # that mapping starts at the song's own page. An unresolvable track gets
        # an honest message and a retry; it never gets a menu of formats no
        # source has been found to deliver. Its length is also what puts a size
        # on every bitrate row and on the card.
        try:
            duration = float((await spotify.lookup(url)).duration_s or 0)
        except Exception:
            logger.warning("spotify lookup gave nothing for %.80s", url)
            await _ask_again(
                message,
                state,
                url,
                lang,
                t("intake.spotify_unresolved", lang),
                edit=edit,
            )
            return
        # Honest about the resolver in front of the menu: the file comes from
        # the mapped source, and the quality follows it.
        prompt = f"{t(routing.header_key, lang)}\n\n{t('intake.spotify_note', lang)}"
    else:
        await _typing(bot, message.chat.id)
        async with ActionPulse(bot, message.chat.id, ChatAction.TYPING):
            info = await _probe_meta(bot, url)
        duration = float(info.duration or 0) if info is not None else 0.0
    title = _clean_title(info, url)
    options = tuple(info.video_options) if info is not None else ()
    if routing.kind == "video" and not options:
        # The reported bug, pinned to one place: a video link whose qualities
        # were not discovered says so and offers a retry (which re-extracts).
        # The automatic row is drawn only when an operator deliberately enabled
        # it (MENU_AUTO_BEST) — and it names itself an automatic pick, never an
        # exact quality.
        await _ask_again(
            message,
            state,
            url,
            lang,
            t("intake.probe_failed", lang),
            edit=edit,
            title=title,
            auto_best=get_settings().menu_auto_best,
        )
        return
    # One capability model, built from what the probe found, drives every screen
    # this link can reach — the format grid, the bitrate rows and their sizes.
    capability = audio_capability(
        source_ext=(info.audio_ext if info is not None else None),
        duration_s=duration,
        upload_limit_bytes=get_settings().upload_limit_bytes,
    )
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(
        url=url,
        title=title,
        duration=duration,
        source_kbps=(info.audio_kbps if info is not None else None),
        source_kbps_approx=(info.audio_kbps_approx if info is not None else False),
        offered=_offered_tiers(url, options),
        audio_offered=(list(capability.formats) if routing.audio_formats else None),
        copy_ok=capability.copy_ok,
        live=(info.is_live if info is not None else None),
        size_guess=(info.filesize_approx if info is not None else None),
        options=[
            (option.height, option.label_p, option.size_bytes, option.size_exact)
            for option in options
        ],
    )
    text = _question_text(url, lang, title=title, prompt=prompt)
    keyboard = _question_keyboard(url, lang, options=options, capability=capability)
    if edit:
        await _edit_or_reply(
            message, text, reply_markup=keyboard, link_preview_options=_NO_PREVIEW
        )
    else:
        await _send_question(
            message,
            text,
            keyboard,
            thumbnail=(info.thumbnail if info is not None else None),
            caption=_photo_caption(
                url, lang, title=title, duration=format_duration(duration), prompt=prompt
            ),
        )


async def _ask_again(
    message: Message,
    state: FSMContext,
    url: str,
    lang: str,
    prompt: str,
    *,
    edit: bool,
    title: str = "",
    auto_best: bool = False,
) -> None:
    """The honest "could not read this link" screen: retry, or leave.

    Shared by every capability lookup that came back empty (no qualities, no
    resolvable track). The retry button re-enters the probe (``on_probe_retry``)
    — a stale or failed lookup is re-extracted, never papered over. The one
    download-shaped button here is the deliberately opt-in automatic pick, and
    it is spelled as an automatic pick; whatever is *not* drawn is the point.
    """
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(
        url=url,
        title=title,
        offered=(["best"] if auto_best else []),
        options=[],
    )
    text = _question_text(url, lang, title=title, prompt=prompt)
    keyboard = _probe_retry_keyboard(lang, auto_best=auto_best)
    if edit:
        await _edit_or_reply(
            message, text, reply_markup=keyboard, link_preview_options=_NO_PREVIEW
        )
    else:
        await message.answer(text, reply_markup=keyboard, link_preview_options=_NO_PREVIEW)


def _probe_retry_keyboard(lang: str, *, auto_best: bool = False) -> InlineKeyboardMarkup:
    """Retry the probe — and, only when deliberately enabled, the automatic row."""
    builder = InlineKeyboardBuilder()
    builder.button(text=t("intake.probe_retry_btn", lang), callback_data=PROBE_CALLBACK)
    if auto_best:
        builder.button(
            text=t("intake.auto_best_btn", lang),
            callback_data=_fmt_callback("video", "best"),
        )
    builder.button(text=t("menu.back", lang), callback_data="menu:download")
    builder.adjust(1)
    return builder.as_markup()


def _request_parts(request: str) -> tuple[str, str]:
    """A stored request key back into the tap vocabulary.

    ``"audio:mp3.best"`` → ``("audio", "mp3.best")``; a request with no tier
    names its format's default — the exact spelling ``request_key`` folds it
    back to, so a cached menu's tap is the very request the row remembers.
    """
    media_format, _, tier = (request or "").partition(":")
    return media_format, tier or default_quality(media_format)


async def _cached_rows(pool: asyncpg.Pool, url: str) -> list[Any]:
    """The rows this URL already produced — or none.

    The fast path is a courtesy and must never cost the question anything: a
    lookup that raises is a miss (the ordinary probe runs instead), and a row
    that speaks no request language is not menu material.
    """
    try:
        rows = await cache_service.get_cached_rows(pool, url)
    except Exception:  # the cache is never worth an error screen
        logger.debug("cache lookup gave nothing for %.80s — probing as usual", url, exc_info=True)
        return []
    return [row for row in rows if str(row["quality"] or "").partition(":")[0]]


def _cached_question_keyboard(rows: Sequence[Any], lang: str) -> InlineKeyboardMarkup:
    """One button per stored request — the same ``fmt:`` vocabulary as any menu.

    Each label is the one the fresh caption used, so the screen looks like the
    menu it stands in for. The full probed ladder is one button away — a quality
    nobody has taken yet is a re-check, never a dead end — and the way out leads
    to the Download screen, like every question.
    """
    builder = InlineKeyboardBuilder()
    for row in rows:
        request = str(row["quality"] or "")
        media_format, tier = _request_parts(request)
        builder.button(
            text=str(row["label"] or "") or label_for_request(request, lang),
            callback_data=_fmt_callback(media_format, tier),
        )
    builder.button(text=t("intake.full_menu_btn", lang), callback_data=PROBE_CALLBACK)
    builder.button(text=t("menu.back", lang), callback_data="menu:download")
    builder.adjust(1)
    return builder.as_markup()


async def _ask_from_cache(
    message: Message,
    state: FSMContext,
    url: str,
    lang: str,
    rows: Sequence[Any],
    *,
    edit: bool,
) -> None:
    """The question built from what the link already produced — no probe, no wait.

    Each row is a request that has been served once and stored, so its button is
    the same tap a fresh menu would draw and its press replays the file instantly
    instead of downloading again. What the rows know (the title) is shown; what
    they do not is simply not there. The FSM carries the offered vocabulary a tap
    is judged by, so this menu speaks exactly the language ``on_format_chosen``
    validates — and a crafted tap on a button that was never drawn buys nothing.
    """
    title = str(rows[0]["title"] or "").strip()
    if title == url.strip():
        title = ""
    offered: list[str] = []
    audio_offered: list[str] = []
    for row in rows:
        media_format, tier = _request_parts(str(row["quality"] or ""))
        if media_format == "video":
            offered.append(tier)
        elif media_format == "audio":
            codec = tier.split(".", 1)[0]
            if codec not in audio_offered:
                audio_offered.append(codec)
    text = _question_text(url, lang, title=title)
    keyboard = _cached_question_keyboard(rows, lang)
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(
        url=url,
        title=title,
        duration=0.0,
        options=[],
        offered=offered,
        audio_offered=audio_offered,
    )
    if edit:
        await _edit_or_reply(
            message, text, reply_markup=keyboard, link_preview_options=_NO_PREVIEW
        )
    else:
        await message.answer(text, reply_markup=keyboard, link_preview_options=_NO_PREVIEW)


def _question_body(url: str, lang: str, *, prompt: str | None = None) -> str:
    """The one line asking — what every question screen ends with."""
    return prompt if prompt is not None else t(content.routing_for(url).header_key, lang)


def _question_text(
    url: str, lang: str, *, title: str = "", prompt: str | None = None
) -> str:
    """The card, then the one line asking — never two messages.

    The card's 🎞 line is *deliberately* absent here: nothing has been chosen yet.
    It appears the moment a choice is made, naming it.
    """
    card = media_card(title=title, url=url, lang=lang)
    text = _question_body(url, lang, prompt=prompt)
    return f"{card}\n\n{text}" if card else text


def _photo_caption(
    url: str, lang: str, *, title: str, duration: str, prompt: str | None = None
) -> str:
    """The question as a photo's caption: what this is, how long, then the ask.

    The picture answers «what is this» better than any card can, so the caption
    does not repeat the link — it names the title and the length (bold, and each
    omitted when the site reported none) and asks its one question underneath.
    """
    facts = [
        line
        for line in (
            t("media.line_title", lang, title=f"<b>{escape_html(title.strip())}</b>")
            if title.strip()
            else "",
            t("media.line_duration", lang, duration=f"<b>{escape_html(duration.strip())}</b>")
            if duration.strip()
            else "",
        )
        if line
    ]
    body = _question_body(url, lang, prompt=prompt)
    return "\n\n".join(["\n".join(facts), body] if facts else [body])


async def _send_question(
    message: Message,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    thumbnail: str | None,
    caption: str,
) -> None:
    """Ask with the link's own picture — never at the cost of the question.

    A thumbnail Telegram will not carry (a dead URL, a picture it refuses) must
    not eat the menu: the photo attempt is best-effort, and the fallback is the
    same text screen this has always been.
    """
    if thumbnail:
        try:
            await message.answer_photo(photo=thumbnail, caption=caption, reply_markup=keyboard)
            return
        except Exception:  # a refused picture is the fallback's cue, never an error
            logger.warning("thumbnail %.80s would not send — asking as text", thumbnail)
    await message.answer(text, reply_markup=keyboard, link_preview_options=_NO_PREVIEW)


def _clean_title(info: MediaInfo | None, url: str) -> str:
    """A title worth its 🎬 line — or ``""`` so the line is simply not there."""
    title = (info.title if info is not None else "").strip()
    return "" if not title or title == url.strip() else title


def _offered_tiers(url: str, options: Sequence[VideoOption]) -> list[str]:
    """Exactly what the question's buttons say a tap may choose — the vocabulary
    the tap is later validated against (a crafted callback is data)."""
    if options:
        tiers = [str(option.height) for option in options]
        routing = content.routing_for(url)
        if routing.media_choice is not None:
            # A post link's question carries its "send the media" button
            # alongside the ladder — that request belongs to the vocabulary too.
            tiers.append(str(routing.media_choice.quality))
        return tiers
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


#: The probe's wait budget when the extractor carries no timeout of its own.
#: Real extractors always carry one (``timeout_s``) and the budget defers to it:
#: clipping the wait *shorter* than the extraction's own contract cancelled the
#: probe while the extraction kept running in its thread — a valid format list
#: arrived too late to draw the menu from, and every slow link silently degraded
#: to the single «download» row. The slack covers the round trip around it.
_META_PROBE_TIMEOUT_S = 5.0
_META_PROBE_SLACK_S = 5.0

#: How long a share/short link may take to say where it really points.
_CANONICAL_RESOLVE_S = 8.0

#: A browser's user agent — share pages are pages, and some of them answer a
#: nameless client with a block page instead of a redirect.
_PROBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


async def _canonical_url(url: str) -> str:
    """Turn a share/viewer link into the page or file it names — or leave it be.

    A `/s/ShareToken` link is not a page at all: it redirects to Reddit's media
    *viewer* (`/media?url=…`), a wrapper no extractor handles, while the URL
    inside it is a plain downloadable file. Resolving once at intake is what lets
    routing, the engines and the direct-download path all see the real thing.
    Only links the extractor catalogue cannot claim pay for the round trip, and
    any failure (blocked, slow, offline) keeps the original link — the engines
    and the fallback still get their turn on it.
    """
    unwrapped = content.unwrap_media_url(url)
    if unwrapped != url or not content.claims_platform(url):
        return unwrapped
    if ExtractorService.is_url_supported(url):
        return url
    try:
        timeout = aiohttp.ClientTimeout(total=_CANONICAL_RESOLVE_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, allow_redirects=True, headers={"User-Agent": _PROBE_USER_AGENT}
            ) as response:
                resolved = str(response.url)
    except Exception:
        logger.info("share link %.80s did not resolve — keeping it as-is", url)
        return url
    canonical = content.unwrap_media_url(resolved)
    if canonical != resolved or ExtractorService.is_url_supported(canonical):
        # Worth adopting when the redirect *revealed* something: a media file
        # hiding behind a viewer wrapper, or a page the engines can claim. A
        # cosmetic hop between page forms (youtu.be → watch?v=) changes nothing
        # worth changing — the user's own link stays on the card and in the job.
        if canonical != url:
            logger.info("share link %.80s resolves to %.80s", url, canonical)
        return canonical
    return url


async def _probe_meta(bot: Bot, url: str) -> MediaInfo | None:
    """What the question needs to know: the title, and which resolutions exist.

    Answered by the extraction pipeline's own metadata (``bot.state.extractor``) —
    never a second engine, never a network call of this module's own. The wait is
    the extractor's *own* contract (``timeout_s``): a menu must be drawn from a
    format list that is really there, not one this module cancelled early. A
    probe that still gives nothing (a blocked host, a test double with no
    extractor at all) answers ``None``, and says why in the log — the question is
    still asked, with the tier menu and a card that shows its link.
    """
    extractor = getattr(getattr(bot, "state", None), "extractor", None)
    if extractor is None:
        return None
    budget = float(getattr(extractor, "timeout_s", 0) or 0) or _META_PROBE_TIMEOUT_S
    try:
        return await asyncio.wait_for(
            extractor.extract(url), timeout=budget + _META_PROBE_SLACK_S
        )
    except Exception as exc:
        logger.warning(
            "metadata probe gave nothing for %.80s (%s: %s) — falling back to tiers",
            url,
            type(exc).__name__,
            exc,
        )
        return None


async def _probe_supported(url: str) -> bool:
    """Whether this link may be offered at all — asked generously.

    The bot's own router outranks yt-dlp's URL catalogue: a host this bot
    advertises (Reddit among them) is supported even when the site handler
    yt-dlp ships misses this particular link shape — share links and direct
    media files are exactly those shapes, and the engines and the fallback get
    the final word on them. Only a link none of the layers below recognize is
    honestly "unsupported" before it is queued.
    """
    if spotify.is_spotify_url(url):
        # Served by rewriting the link, not by yt-dlp (see services/spotify.py), so
        # yt-dlp's opinion of it — "known to use DRM protection" — decides nothing.
        return True
    if (
        content.claims_platform(url)
        or content.knows_host(url)
        or content.classify(url) != "media"
    ):
        # A platform this bot advertises (share links and direct media files
        # included), a claimed host, or a link that is itself a file: the
        # download path and the fallback serve these whatever any one
        # extractor's regexes say.
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
    capability = _stored_capability(data)
    if spec == "back":
        await cb.answer()
        await _edit_or_reply(
            message,
            _question_text(url, lang, title=title),
            reply_markup=_question_keyboard(url, lang, capability=capability),
            link_preview_options=_NO_PREVIEW,
        )
        return
    # Deliberately strict (same rule as find_choice): the codec must have been a
    # button on *this* link's question — an audio menu has no business opening
    # for a video link, however the tap was spelled. When the question was built
    # from probed facts, its capability list *is* that vocabulary.
    if (
        spec not in content.routing_for(url).audio_formats
        or not content.audio_level_choices(spec)
        or (capability is not None and spec not in capability.formats)
    ):
        await cb.answer(t("intake.no_format", lang), show_alert=True)
        return
    await cb.answer()
    prompt = t("audio.choose_level_fmt", lang, format=spec.upper())
    notes = [note for note in (_source_rate_note(spec, data, lang),) if note]
    # What a rate row *is*, said once: a conversion target, not a claim about the
    # source — and «Original», when its row is here, is the untouched stream.
    notes.append(t("audio.converted_note", lang))
    prompt = f"{prompt}\n\n" + "\n".join(notes)
    await _edit_or_reply(
        message,
        _question_text(url, lang, title=title, prompt=prompt),
        reply_markup=_level_keyboard(
            spec,
            lang,
            duration=float(data.get("duration") or 0),
            source_kbps=_source_kbps_of(data),
            copy_ok=(capability.copy_ok if capability is not None else True),
            upload_limit_bytes=get_settings().upload_limit_bytes,
        ),
        link_preview_options=_NO_PREVIEW,
    )


@router.callback_query(DownloadStates.waiting_format, F.data == PROBE_CALLBACK)
async def on_probe_retry(
    cb: CallbackQuery,
    state: FSMContext,
    user: asyncpg.Record,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    bot: Bot,
    lang: str = DEFAULT_LANG,
) -> None:
    """The error screen's retry: run the capability lookup again, in place.

    Re-extraction is the point: a menu that could not be drawn because the probe
    failed is drawn again from a *fresh* probe (the link is server-side state in
    the FSM — a crafted callback names at most that). A second failure re-opens
    the same honest screen.
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
    await cb.answer()
    running = (int(user["telegram_id"]), str(url))
    if running in _probes_running:
        # The fresh lookup is already in the air — a spammed retry is answered
        # and dropped, never a second concurrent extraction of the same link.
        return
    _probes_running.add(running)
    try:
        await _ask_about_link(
            message,
            state,
            user,
            str(url),
            lang,
            bot=bot,
            pool=pool,
            queue=queue,
            edit=True,
            force_probe=True,
        )
    finally:
        _probes_running.discard(running)


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
        is_live=data.get("live") if isinstance(data.get("live"), bool) else None,
        size_estimate=(
            int(data["size_guess"]) if isinstance(data.get("size_guess"), int) else None
        ),
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

    Two probed vocabularies and one static — one rule. The router's static menu
    answers through ``find_choice`` (deliberately strict — see its own note). The
    *probed* rows — video resolutions and audio formats alike — are vocabularies
    no table knows, so what the question showed travels in the FSM instead: the
    offered list decides, and a crafted height is still just data
    (``is_video_height`` agrees). When a menu predates the offered lists, the
    static rule is all there is.
    """
    if not media_format or not quality:
        return False
    if media_format == "video" and data.get("offered") is not None:
        # Every video request is judged by the offered list — "best" included.
        # The automatic row is only in that list when the menu really drew it
        # (MENU_AUTO_BEST), so a crafted ``fmt:video:best`` on a failed lookup
        # gets exactly what a crafted height gets: nothing.
        return quality in [str(item) for item in data["offered"]]
    if media_format == "audio" and data.get("audio_offered") is not None:
        # The audio grid is probed too: what the question offered decides — and
        # the untouched-stream row was only offered when its container is honest.
        return (
            quality.split(".", 1)[0] in [str(item) for item in data["audio_offered"]]
            and not (audio_is_original(quality) and data.get("copy_ok") is False)
        )
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
    else's failure at most, and it comes back empty-handed. A key is spent once,
    so a spammed button queues nothing — and the double-tap guard inside
    ``_submit`` drops a second key's press for the same request while the first
    is still running. The retry re-enters the ordinary path — cache, quota and
    preflight all get their say again — but only *after* the failed run's stale
    state has been dropped (see below).
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
    # A retry is a *fresh* extraction, never a replay of the failure it is
    # retrying away from. Two stale things must not answer it first: the
    # request's cache row (a replay would "succeed" without extracting
    # anything) and the remembered "anonymous requests are refused here"
    # verdict — exactly the cached failure state this button exists to shake
    # off. The worker records the verdict again if the refusal is still real
    # (see services/worker.py).
    await cache_service.forget(pool, task.url, task.media_format, task.quality)
    preflight.clear_anonymous_refusal()
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
        is_live=task.is_live,
        size_estimate=task.size_estimate,
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


@router.callback_query(F.data == PROBE_CALLBACK)
async def on_stale_probe_tap(cb: CallbackQuery, lang: str = DEFAULT_LANG) -> None:
    """A retry tap on a screen that is no longer live: the same clear answer
    every stale selection gets — send the link again, and the probe re-runs with
    it. (An unanswered callback spins forever, which is worse than any answer.)"""
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
    is_live: bool | None = None,
    size_estimate: int | None = None,
    fallback_any_tier: bool = False,
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
        message,
        card,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        link_preview_options=_NO_PREVIEW,
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
        is_live=is_live,
        size_estimate=size_estimate,
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

    # 2b) Zero-wait for the auto-best path (solo links): the request has no tier
    #     the user chose, so a stored sibling of the same media family answers it
    #     — "send this post's media" never re-downloads what this URL already
    #     produced just because the earlier ask named a tier. An explicit tap
    #     stays tier-honest (step 2's exact key is all it gets); this wider net is
    #     only for the request where the engine picks and the caption names what
    #     actually arrived. Dead rows are forgotten and the next one tried.
    if fallback_any_tier:
        for row in await _cached_rows(pool, url):
            row_format, row_tier = _request_parts(str(row["quality"] or ""))
            if row_format != media_format:
                continue
            if await send_cached_file(bot, chat_id, row, caption=replay_caption(row, lang)):
                return
            await cache_service.forget(pool, url, row_format, row_tier)

    # 3) Soft daily-quota check (the worker claims the slot atomically).
    usage = await database.get_daily_usage(pool, user["telegram_id"])
    used = usage["daily_downloads"] if usage and usage["last_download_date"] == today_local() else 0
    limit = effective_daily_limit(user)
    if used >= limit:
        await _edit_or_reply(
            message,
            f"{card}\n\n{t('intake.quota_exhausted', lang, used=used, limit=limit)}",
            reply_markup=_back_to_menu(lang, to="menu:download"),
            link_preview_options=_NO_PREVIEW,
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
            link_preview_options=_NO_PREVIEW,
        )
        return

    # 5) Push to the queue — and say nothing: the card already did. The file
    #    arrives as its own message; warnings and receipts are the chat's noise.
    #    The queue is single-flight: the same job already in the air (a second
    #    tap on the same request, the same link sent again mid-download) comes
    #    back as -1 and this trigger is dropped — one request, one download, one
    #    delivery, no matter how many times or how concurrently it is asked for.
    if await queue.enqueue(task) < 0:
        return


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
    await message.answer(t("intake.cancelled", lang), link_preview_options=_NO_PREVIEW)
