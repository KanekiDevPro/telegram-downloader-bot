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

import asyncpg

from core import database
from core.utils import canonical_url, quality_key, sha256_hex

__all__ = ["cache_key", "forget", "get_cached", "memorize", "request_key"]


def request_key(media_format: str = "video", quality: object = "") -> str:
    """The part of a cache key that names what was asked for (format + tier)."""
    return quality_key(media_format, quality)


def cache_key(url: str, media_format: str = "video", quality: object = "") -> str:
    """Stable cache key for ``url`` + the requested format and quality tier."""
    return sha256_hex(f"{canonical_url(url)}|{request_key(media_format, quality)}")


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
) -> None:
    """Store a fresh file_id for a URL after a successful upload.

    ``request`` is the key the lookup used (:func:`request_key`) and is part of the
    hash, not just metadata. ``kind`` is *how the file was sent* — ``photo``,
    ``photo_group``, ``audio``, … — because what comes back is not always what was
    asked for: an image post answered a «video» request, and the cached replay has
    to send it as a photo again. For a gallery, ``telegram_file_id`` holds a JSON
    list of ids.
    """
    await database.store_cached_file(
        pool,
        url_hash=sha256_hex(f"{canonical_url(url)}|{request}"),
        original_url=url,
        platform=platform,
        telegram_file_id=telegram_file_id,
        quality=request,
        kind=kind,
    )


async def forget(
    pool: asyncpg.Pool, url: str, media_format: str = "video", quality: object = ""
) -> None:
    """Remove a (possibly stale) cache entry for this exact request."""
    await database.delete_cached_file(pool, cache_key(url, media_format, quality))
