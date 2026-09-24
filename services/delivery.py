"""Delivery helper: re-send a cached Telegram file by ``file_id``.

Shared by the gateway (instant cache hit) and the workers (duplicate request
that arrived while another worker was downloading). Telegram invalidates
``file_id``s rarely but it does happen, so callers get ``False`` back and are
expected to drop the cache entry and download again.

How to send it back is the cache's business, not the request's: an image post
answers a «video» ask with photos, so the row remembers what the upload actually
*was* (``kind``) and a replay sends it the same way. A gallery keeps its ids as a
JSON list in the one column cache rows have — several pictures are several
``file_id``s, and Telegram wants them together in a media group.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any

import asyncpg
from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

from core.i18n import DEFAULT_LANG, t
from core.utils import default_quality, escape_html, normalize_quality
from services.extractor import audio_bitrate, audio_is_original
from services.verify import upscale_disclaimer

#: What one media group may hold — aiogram's own union, spelled out so a list of
#: ``InputMediaPhoto`` can be handed over without a cast (lists are invariant).
MediaItem = InputMediaAudio | InputMediaDocument | InputMediaLivePhoto | InputMediaPhoto | InputMediaVideo

logger = logging.getLogger(__name__)

#: The bot's own @handle, recorded once at boot (``main.py``) and stamped on every
#: media card. A file saved out of its chat and opened weeks later should still say
#: which bot produced it — but a card never shows a blank field, so before boot (and
#: in tests) the line is simply omitted.
_BOT_USERNAME = ""


def set_bot_username(username: str) -> None:
    """Record what ``get_me`` answered at boot, without the ``@``."""
    global _BOT_USERNAME
    _BOT_USERNAME = (username or "").lstrip("@")


def bot_username() -> str:
    """The handle every card's 🤖 line carries (``""`` before boot)."""
    return _BOT_USERNAME


def group_add_link() -> str:
    """Telegram's own «add me to a group» flow for this bot (``""`` before boot).

    The ``?startgroup`` deep link is what makes Telegram open its group picker —
    the bot's real address with Telegram's documented parameter, nothing invented.
    Before ``get_me`` answers there is no name to build it from, and a wrong link
    is worse than no button at all.
    """
    return f"https://t.me/{_BOT_USERNAME}?startgroup=true" if _BOT_USERNAME else ""


#: Which ephemeral chat action says «working on it» per delivered kind. The Bot
#: API has no "uploading audio" slot — the voice action is its audio upload — so
#: a song pulses that one, and everything else pulses its own shape.
_UPLOAD_ACTIONS: dict[str, ChatAction] = {
    "video": ChatAction.UPLOAD_VIDEO,
    "audio": ChatAction.UPLOAD_VOICE,
    "photo": ChatAction.UPLOAD_PHOTO,
    "photo_group": ChatAction.UPLOAD_PHOTO,
}


def upload_action(kind: str) -> ChatAction:
    """The upload indicator for a delivery shape (documents for anything else)."""
    return _UPLOAD_ACTIONS.get(kind, ChatAction.UPLOAD_DOCUMENT)


class ActionPulse:
    """An ephemeral «alive» indicator while work runs: Telegram chat actions.

    Not a message — nothing lands in the chat, so the two-message contract holds.
    One bounded task sends the action every few seconds (Telegram drops it after
    ~5s, so the heartbeat keeps the indicator lit) and is cancelled with the
    work: success, failure and shutdown all stop it, and nothing survives the
    media. A grace delay before the first action means a short job never
    flickers the indicator at all.
    """

    #: Before the first action: shorter jobs are done before anyone notices.
    GRACE_S = 1.5
    #: Between actions: Telegram's own indicator expires in ~5 seconds.
    HEARTBEAT_S = 4.0

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        action: ChatAction = ChatAction.TYPING,
        *,
        grace: float | None = None,
        heartbeat: float | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._action = action
        self._grace = self.GRACE_S if grace is None else grace
        self._heartbeat = self.HEARTBEAT_S if heartbeat is None else heartbeat
        self._task: "asyncio.Task[None] | None" = None

    async def __aenter__(self) -> "ActionPulse":
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def stop(self) -> None:
        """Cancel the pulse and wait for it to die — idempotent."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        await asyncio.sleep(self._grace)
        while True:
            try:
                await self._bot.send_chat_action(self._chat_id, self._action)
            except Exception:  # noqa: BLE001 — the indicator is best-effort, never fatal
                # A chat that refuses actions — or a bot stub without them — is
                # not worth another attempt, and certainly not worth failing the
                # download over.
                return
            await asyncio.sleep(self._heartbeat)


def format_duration(seconds: float | int | None) -> str:
    """``150`` → ``"2:30"``, ``3725`` → ``"1:02:05"`` — ``""`` when unknown.

    A song's length said the way a player says it, or not at all.
    """
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}" if hours else f"{minutes}:{sec:02d}"


def media_card(
    *,
    title: str = "",
    url: str = "",
    quality: str = "",
    size: str = "",
    audio: bool = False,
    artist: str = "",
    album: str = "",
    duration: str = "",
    lang: str = DEFAULT_LANG,
) -> str:
    """The standard block for anything downloadable — screen or caption alike.

    A video answers "what is this" in four lines: 🎬 what it is, 🎞 which version
    this is (quality and, when it is known, its size), 🔗 where it came from (the
    link *the user sent*, so a Spotify track names the Spotify URL), 🤖 who made
    it. A song speaks its own language instead — 🎵 the title, 🎤 who made it and
    💿 which release, ⏱ how long and 🎧 what this file is — because music is
    identified by its credits, not by a filename. Related facts touch; groups
    breathe one blank line apart, so the block scans on a phone. A missing fact
    omits its line — never «None», never an empty field — so a card read at any
    moment is complete.
    """
    clean_title = title.strip()
    identity: list[str] = []
    if clean_title and clean_title != url.strip():
        # A title that *is* the URL (the extractor's own fallback) would say it
        # twice; the 🔗 line already carries it.
        key = "media.line_music" if audio else "media.line_title"
        identity.append(t(key, lang, title=escape_html(clean_title[:120])))
    credits = [
        line
        for line in (
            t("media.line_artist", lang, artist=escape_html(artist.strip()))
            if artist.strip()
            else "",
            t("media.line_album", lang, album=escape_html(album.strip()))
            if album.strip()
            else "",
        )
        if line
    ]
    quality_line = ""
    if quality or size:
        # Quality and size are one fact — which version this is — on one line.
        if quality and size:
            shown = f"{quality} · {size}" if audio else f"{quality} • {size}"
        else:
            shown = quality or size
        key = "media.line_audio_quality" if audio else "media.line_quality"
        quality_line = t(key, lang, quality=escape_html(shown))
    sound = [
        line
        for line in (
            t("media.line_duration", lang, duration=escape_html(duration.strip()))
            if duration.strip()
            else "",
            quality_line,
        )
        if line
    ]
    source = [t("media.line_url", lang, url=escape_html(url))] if url else []
    made = (
        [t("media.line_bot", lang, bot=escape_html(f"@{_BOT_USERNAME}"))]
        if _BOT_USERNAME
        else []
    )
    groups = (
        [identity, credits, sound, source, made]
        if audio
        else [identity, sound, source, made]
    )
    return "\n\n".join("\n".join(group) for group in groups if group)


def resolution_name(height: object, lang: str = DEFAULT_LANG) -> str:
    """``1080p`` — and the two names cinema gave the big ones: ``1440p (2K)``,
    ``2160p (4K)``. The number is the fact; the tag only helps place it.
    """
    try:
        value = int(str(height).strip())
    except (TypeError, ValueError):
        return ""
    name = t("media.quality_p", lang, height=value)
    if value >= 2160:
        return f"{name} (4K)"
    if value >= 1440:
        return f"{name} (2K)"
    return name


#: The container each audio codec's file is named by — and what a produced file's
#: extension says it *is*.
_AUDIO_EXT_NAMES: dict[str, str] = {
    ".mp3": "MP3",
    ".m4a": "M4A",
    ".aac": "M4A",
    ".opus": "OPUS",
    ".ogg": "OPUS",
    ".wav": "WAV",
    ".flac": "FLAC",
}
_TIER_EXT: dict[str, str] = {
    "mp3": ".mp3",
    "m4a": ".m4a",
    "aac": ".m4a",
    "opus": ".opus",
    "wav": ".wav",
    "flac": ".flac",
}


def produced_quality_label(
    media_format: str,
    quality: object,
    suffix: str,
    lang: str = DEFAULT_LANG,
    *,
    produced_p: object = None,
    source_kbps: int | None = None,
) -> str:
    """The quality line for a file that exists — named by what it actually is.

    Video: the resolution the produced file reports, and *nothing* when nobody
    reported one ("best available" is a ranking, not a description). Audio: the
    container the file system says — ``.flac`` is FLAC whatever button folklore
    remembers — with the bitrate named only when the codec asked for is the codec
    that happened. Never «320 kbps» under a file that is something else.

    ``source_kbps`` is the source's own lossy rate when the extraction reported
    one: a rate *above* it is the encoder's target, not the source's quality, so
    the label names the source too (``media.upscale_mark``, via
    :func:`services.verify.upscale_disclaimer`) — a true number over a weaker
    source must not read as a quality claim. An unknown source rate marks
    nothing: nothing is invented either way.
    """
    if media_format != "audio":
        return resolution_name(produced_p, lang) if produced_p else ""
    tier = normalize_quality(quality, "audio")
    suffix = (suffix or "").lower()
    name = _AUDIO_EXT_NAMES.get(suffix) or tier.split(".")[0].upper()
    if audio_is_original(tier):
        return f"{name} · {t('media.original', lang)}"
    kbps = audio_bitrate(tier)
    if kbps and suffix == _TIER_EXT.get(tier.split(".")[0]):
        label = f"{name} · {kbps} kbps"
        upscale = upscale_disclaimer("audio", tier, source_kbps)
        if upscale is not None:
            label = f"{label} · {t('media.upscale_mark', lang, source=upscale[1])}"
        return label
    return name


def quality_label(media_format: str, quality: object, lang: str = DEFAULT_LANG) -> str:
    """What the quality line says: ``1080p``, ``MP3 · 320 kbps``, ``M4A · Original``…

    Audio names its *rate*, not a mood: the file is exactly the kbps the button
    promised — or the site's own untouched stream, said so in plain words. Raw and
    lossless output simply name their container: no knob, no claim. A default
    video request names nothing: what arrived is described by
    :func:`produced_quality_label` once it exists.
    """
    if media_format != "audio":
        tier = normalize_quality(quality, "video")
        return "" if tier == "best" else resolution_name(tier, lang)
    tier = normalize_quality(quality, "audio")
    name = tier.split(".")[0].upper()
    kbps = audio_bitrate(tier)
    if kbps:
        return f"{name} · {kbps} kbps"
    if audio_is_original(tier):
        return f"{name} · {t('media.original', lang)}"
    return name


def label_for_request(request: str, lang: str = DEFAULT_LANG) -> str:
    """The 🎞 text of a cache row's request key (``"audio:mp3.high"``, ``"video"``).

    The request key is all a replay knows about *which* version this file is —
    ``quality_key``'s spelling (``format`` alone for the default tier, ``format:tier``
    for a deliberate one), so the label goes through the same normalisation the
    rest of the pipeline uses.
    """
    media_format, _, tier = (request or "").partition(":")
    if media_format not in ("audio", "video"):
        return ""
    return quality_label(media_format, tier or default_quality(media_format), lang)


def replay_caption(record: asyncpg.Record, lang: str = DEFAULT_LANG) -> str:
    """The caption a replayed cache hit gets: the same media card as a fresh send.

    A cached file is the same file, so it gets the same caption. Whether the bot has
    seen a link before is an implementation detail, and it must not be visible in
    the chat. The row knows the request, the original link and (since the column
    arrived) the title — an older row simply omits that line rather than faking
    one.
    """
    request = _field(record, "quality")
    return media_card(
        title=_field(record, "title"),
        url=_field(record, "original_url"),
        # The label the fresh caption used, stored verbatim — the replay's card is
        # the first send's card. A row from before that column describes its
        # request instead.
        quality=_field(record, "label") or label_for_request(request, lang),
        audio=request.startswith("audio"),
        lang=lang,
    )

#: Telegram's own ceiling on one media group; a larger album is sent in batches.
MEDIA_GROUP_MAX = 10

#: The formats that decide how a row written *before* ``kind`` existed is re-sent.
_LEGACY_KINDS = frozenset({"audio", "video"})

__all__ = [
    "ActionPulse",
    "bot_username",
    "format_duration",
    "group_add_link",
    "label_for_request",
    "MEDIA_GROUP_MAX",
    "media_card",
    "join_file_ids",
    "quality_label",
    "replay_caption",
    "send_album",
    "send_cached_file",
    "set_bot_username",
    "split_file_ids",
    "upload_action",
]


def split_file_ids(stored: str) -> list[str]:
    """The ids behind a cache row: one id, or the JSON list of a gallery."""
    text = (stored or "").strip()
    if not text:
        return []
    if not text.startswith("["):
        return [text]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("cache row holds an unreadable file_id list: %.60s", text)
        return []
    return [str(item) for item in parsed if isinstance(item, str) and item]


def join_file_ids(ids: Sequence[str]) -> str:
    """Store a gallery's ids in the single column cache rows have."""
    return json.dumps(list(ids))


def _field(row: Any, name: str) -> str:
    """One column of a cache row — empty when the row predates it (or is a fake).

    Takes a database record, a mapping or a namedtuple-shaped stand-in: tests
    hand over plain dicts, the worker hands over asyncpg rows.
    """
    with contextlib.suppress(KeyError, IndexError, TypeError):
        return str(row[name] or "")
    with contextlib.suppress(AttributeError):
        return str(getattr(row, name) or "")
    return ""


def _kind_of(row: Any) -> str:
    """How this row has to be re-sent.

    The new column when it is set, else the format that was asked for — rows
    written before ``kind`` existed carry NULL, and sending an old cached video as
    a document would be a regression dressed up as a migration.
    """
    kind = _field(row, "kind").strip()
    if kind:
        return kind
    quality = _field(row, "quality")
    return quality if quality in _LEGACY_KINDS else "file"


async def send_album(
    bot: Bot,
    chat_id: int,
    photos: Sequence[InputMediaPhoto],
    caption: str = "",
) -> list[Message]:
    """Send photos as media groups of Telegram's maximum size, in order.

    The caption goes on the first photo of the first group (Telegram shows each
    group as its own message, so repeating it would shout), and the sent messages
    come back so a caller can cache the ids it just created.
    """
    sent: list[Message] = []
    for index in range(0, len(photos), MEDIA_GROUP_MAX):
        batch: list[MediaItem] = []
        batch.extend(photos[index : index + MEDIA_GROUP_MAX])
        if index == 0 and caption:
            # aiogram's input-media models are frozen, so the caption is a *copy*.
            batch[0] = batch[0].model_copy(update={"caption": caption})
        sent += await bot.send_media_group(chat_id, media=batch)
    return sent


async def send_cached_file(
    bot: Bot,
    chat_id: int,
    cached: asyncpg.Record,
    caption: str | None = None,
    *,
    lang: str = DEFAULT_LANG,
) -> bool:
    """Send a cached file, falling back to a document on type mismatch.

    ``caption`` defaults to the media card a replay gets in ``lang``; callers that
    know the user's language should pass it (both of them do).

    Returns False when the stored ``file_id`` is no longer usable.
    """
    if caption is None:
        caption = replay_caption(cached, lang)
    ids = split_file_ids(_field(cached, "telegram_file_id"))
    if not ids:
        logger.info("cache row %s holds no usable file_id — dropping entry", _field(cached, "url_hash"))
        return False
    kind = _kind_of(cached)
    try:
        if kind == "photo_group":
            await send_album(bot, chat_id, [InputMediaPhoto(media=file_id) for file_id in ids], caption)
        elif kind == "photo":
            await bot.send_photo(chat_id, ids[0], caption=caption)
        elif kind == "audio":
            await bot.send_audio(chat_id, ids[0], caption=caption)
        elif kind == "video":
            await bot.send_video(chat_id, ids[0], caption=caption)
        else:
            await bot.send_document(chat_id, ids[0], caption=caption)
        return True
    except TelegramBadRequest:
        try:
            await bot.send_document(chat_id, ids[0], caption=caption)
            return True
        except TelegramBadRequest:
            logger.info(
                "cached file_id for %s is no longer valid — dropping entry",
                _field(cached, "url_hash"),
            )
            return False
