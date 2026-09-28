"""Smart-cache service: URL → telegram_file_id so repeat requests skip re-downloads.

Cache keys are SHA-256 of the *canonicalized* URL (fragment + tracking params
stripped) **plus the request**, so:

* the same video shared with different UTM tags still hits the cache,
* an MP3 request never receives a cached video for the same link (and vice versa),
* a 480p ask never replays a 1080p file — while the *default* tier keeps the key
  older rows already own, so a tier-aware bot does not orphan a cache that was
  filled before tiers existed.

The request part is spelled by :func:`core.utils.quality_key` (``video``,
``audio``, ``video:480``, ``audio:m4a``), and the *values* in the DB keep saying
what the user asked for (``audio``) next to how it was actually delivered (``kind``).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import asyncpg

from core import database
from core.utils import canonical_url, quality_key, sha256_hex
from services.extractor import VideoOption

__all__ = [
    "cache_key",
    "forget",
    "get_cached",
    "get_cached_rows",
    "memorize",
    "parse_ladder",
    "request_key",
    "serialize_ladder",
    "url_key",
]


def request_key(media_format: str = "video", quality: object = "") -> str:
    """The part of a cache key that names what was asked for (format + tier)."""
    return quality_key(media_format, quality)


def url_key(url: str) -> str:
    """The key that names a *URL* across every request it has ever served.

    The per-request key (:func:`cache_key`) folds the request in and cannot
    answer "what has this link produced?" — the question the intake asks before
    spending an extraction on a link the cache already knows.
    """
    return sha256_hex(canonical_url(url))


def cache_key(url: str, media_format: str = "video", quality: object = "") -> str:
    """Stable cache key for ``url`` + the requested format and quality tier."""
    return sha256_hex(f"{canonical_url(url)}|{request_key(media_format, quality)}")


def serialize_ladder(options: Sequence[VideoOption]) -> str:
    """The full option ladder as stored JSON — ``height, size_bytes,
    size_exact, width`` per rung, the four fields a fresh menu's button is
    drawn from.

    Empty input stores nothing: a row with no ladder is exactly what a row
    from before the column existed looks like, and both keep the rows-only
    menu.
    """
    if not options:
        return ""
    return json.dumps([[o.height, o.size_bytes, o.size_exact, o.width] for o in options])


def parse_ladder(raw: object) -> tuple[VideoOption, ...]:
    """A stored ladder back into options — or ``()`` when there is none.

    A missing column, an empty cell and a foreign blob all read the same
    way: "this row carries no ladder", which is all the instant menu needs
    from a row it cannot draw a rung from.
    """
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        rows = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(rows, list):
        return ()
    rungs: list[VideoOption] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            return ()
        try:
            height, size_bytes, size_exact, width = row
            rungs.append(VideoOption(int(height), int(size_bytes), bool(size_exact), int(width)))
        except (TypeError, ValueError):
            return ()
    return tuple(rungs)


async def get_cached_rows(pool: asyncpg.Pool, url: str) -> list[asyncpg.Record]:
    """Every request this URL has already produced — the zero-wait menu's material.

    What the *intake* asks before any extraction: a link whose answers are
    already stored needs no metadata probe to be asked about again. Rows written
    before the URL key existed are simply not found here, and the ordinary probe
    path runs instead.
    """
    return await database.get_cached_files_for_url(pool, url_key(url))


async def get_cached(
    pool: asyncpg.Pool, url: str, media_format: str = "video", quality: object = ""
) -> asyncpg.Record | None:
    """Return the cached file entry for this exact request, or None."""
    return await database.get_cached_file(pool, cache_key(url, media_format, quality))


async def memorize(
    pool: asyncpg.Pool,
    *,
    url: str,
    platform: str,
    telegram_file_id: str,
    request: str,
    kind: str = "",
    title: str = "",
    label: str = "",
    ladder: Sequence[VideoOption] = (),
) -> None:
    """Store a fresh file_id for a URL after a successful upload.

    ``request`` is the key the lookup used (:func:`request_key`) and is part of the
    hash, not just metadata. ``kind`` is *how the file was sent* — ``photo``,
    ``photo_group``, ``audio``, … — because what comes back is not always what was
    asked for: an image post answered a «video» request, and the cached replay has
    to send it as a photo again. For a gallery, ``telegram_file_id`` holds a JSON
    list of ids. ``title`` is the media's own name and ``label`` the quality line
    the fresh caption used, kept so a replay's card is byte-for-byte the card the
    first send had. ``ladder`` is the full option ladder the send was chosen
    from — stored so the next ask can draw every rung with zero network
    (see :func:`serialize_ladder`).
    """
    await database.store_cached_file(
        pool,
        url_hash=sha256_hex(f"{canonical_url(url)}|{request}"),
        original_url=url,
        platform=platform,
        telegram_file_id=telegram_file_id,
        quality=request,
        kind=kind,
        title=title,
        label=label,
        url_key=url_key(url),
        ladder=serialize_ladder(ladder),
    )


async def forget(
    pool: asyncpg.Pool, url: str, media_format: str = "video", quality: object = ""
) -> None:
    """Remove a (possibly stale) cache entry for this exact request."""
    await database.delete_cached_file(pool, cache_key(url, media_format, quality))
