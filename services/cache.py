"""Smart-cache service: URL → telegram_file_id so repeat requests skip re-downloads.

Cache keys are SHA-256 of the *canonicalized* URL (fragment + tracking params
stripped) **plus the requested format**, so:

* the same video shared with different UTM tags still hits the cache, and
* an MP3 request never receives a cached video for the same link (and vice versa).
"""

from __future__ import annotations

import asyncpg

from core import database
from core.utils import canonical_url, sha256_hex

__all__ = ["get_cached", "memorize", "forget", "cache_key"]


def cache_key(url: str, media_format: str = "video") -> str:
    """Stable cache key for ``url`` + ``media_format`` (video / audio / ...)."""
    return sha256_hex(f"{canonical_url(url)}|{media_format}")


async def get_cached(
    pool: asyncpg.Pool, url: str, media_format: str = "video"
) -> asyncpg.Record | None:
    """Return the cached file entry for a URL + format, or None."""
    return await database.get_cached_file(pool, cache_key(url, media_format))


async def memorize(
    pool: asyncpg.Pool,
    *,
    url: str,
    platform: str,
    telegram_file_id: str,
    quality: str,
    kind: str = "",
) -> None:
    """Store a fresh file_id for an URL after a successful upload.

    ``quality`` is the requested format (``video`` / ``audio``) and is part of
    the cache key, not just metadata. ``kind`` is *how the file was sent* —
    ``photo``, ``photo_group``, ``audio``, … — because what comes back is not
    always what was asked for: an image post answered a «video» request, and the
    cached replay has to send it as a photo again. For a gallery,
    ``telegram_file_id`` holds a JSON list of ids.
    """
    await database.store_cached_file(
        pool,
        url_hash=cache_key(url, quality),
        original_url=url,
        platform=platform,
        telegram_file_id=telegram_file_id,
        quality=quality,
        kind=kind,
    )


async def forget(pool: asyncpg.Pool, url: str, media_format: str = "video") -> None:
    """Remove a (possibly stale) cache entry."""
    await database.delete_cached_file(pool, cache_key(url, media_format))
