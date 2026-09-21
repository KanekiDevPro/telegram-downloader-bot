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

import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

#: What one media group may hold — aiogram's own union, spelled out so a list of
#: ``InputMediaPhoto`` can be handed over without a cast (lists are invariant).
MediaItem = InputMediaAudio | InputMediaDocument | InputMediaLivePhoto | InputMediaPhoto | InputMediaVideo

logger = logging.getLogger(__name__)

CACHED_CAPTION = "📥 ارسال از حافظهٔ کش (دانلود قبلی) ⚡️"

#: Telegram's own ceiling on one media group; a larger album is sent in batches.
MEDIA_GROUP_MAX = 10

#: The formats that decide how a row written *before* ``kind`` existed is re-sent.
_LEGACY_KINDS = frozenset({"audio", "video"})

__all__ = [
    "CACHED_CAPTION",
    "MEDIA_GROUP_MAX",
    "join_file_ids",
    "send_album",
    "send_cached_file",
    "split_file_ids",
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
    """One column of a cache row — empty when the row predates it (or is a fake)."""
    with contextlib.suppress(KeyError, IndexError, TypeError):
        return str(row[name] or "")
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
    caption: str = CACHED_CAPTION,
) -> bool:
    """Send a cached file, falling back to a document on type mismatch.

    Returns False when the stored ``file_id`` is no longer usable.
    """
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
