"""Small shared helpers: hashing, URL handling, formatting, safe filenames."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, timezone, tzinfo
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import get_settings

#: What a user can ask for: the full video, or just the audio track.
MediaFormat = Literal["video", "audio"]

#: How good. A video tier is a *ceiling* (``bestvideo[height<=N]``), never an
#: upscale — asking for 1080p on a 480p video gets the 480p file, which is what
#: "up to 1080p" means on the button. ``m4a`` is the untouched audio stream
#: (no re-encode, faster, plays everywhere Telegram does); ``mp3`` is ffmpeg's.
Quality = Literal["best", "1080", "720", "480", "mp3", "m4a"]
VIDEO_QUALITIES: tuple[Quality, ...] = ("best", "1080", "720", "480")
AUDIO_QUALITIES: tuple[Quality, ...] = ("mp3", "m4a")

#: What a request means when the user did not pick a tier: today's behaviour, and
#: therefore also the cache key an existing row already owns.
DEFAULT_VIDEO_QUALITY: Quality = "best"
DEFAULT_AUDIO_QUALITY: Quality = "mp3"


def default_quality(media_format: str) -> Quality:
    """The tier a request falls back to for that media type."""
    return DEFAULT_AUDIO_QUALITY if media_format == "audio" else DEFAULT_VIDEO_QUALITY


def normalize_quality(value: object, media_format: str = "video") -> Quality:
    """Coerce a tier (a button tap, a queue payload, a DB value) to a valid one.

    An unknown or absent quality is the default for the *format*, never an error:
    an old queued task carries no tier at all, and re-downloading it at "best" is
    exactly what it asked for.
    """
    text = str(value or "").strip().lower()
    allowed = AUDIO_QUALITIES if media_format == "audio" else VIDEO_QUALITIES
    for candidate in allowed:
        if text == candidate:
            return candidate
    return default_quality(media_format)


def quality_key(media_format: str, quality: object) -> str:
    """The part of a cache key that names the *request*.

    The default tier has to keep producing the key older rows already have
    (``url|video``), otherwise a redeploy would silently orphan the whole cache and
    re-download everything once. Only a deliberate tier adds a suffix.
    """
    tier = normalize_quality(quality, media_format)
    return media_format if tier == default_quality(media_format) else f"{media_format}:{tier}"

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# Query parameters that never change the actual media → safe to drop when hashing.
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"}


def sha256_hex(text: str) -> str:
    """SHA-256 hex digest (64 chars) — the smart_cache primary key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_url(url: str) -> str:
    """Normalize a URL for cache-key purposes: drop fragment + tracking params."""
    parsed = urlparse(url)
    if parsed.query:
        kept = [kv for kv in parsed.query.split("&") if kv.split("=", 1)[0].lower() not in _TRACKING_PARAMS]
        query = "&".join(kept)
    else:
        query = ""
    return parsed._replace(fragment="", query=query).geturl()


def extract_url(text: str) -> str | None:
    """Return the first http(s) URL found in a text, or None."""
    match = URL_RE.search(text or "")
    return match.group(0) if match else None


def validate_url(url: str) -> bool:
    """Cheap structural validation: scheme + host present."""
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_local() -> date:
    """Today's date in the configured timezone (used for daily quotas)."""
    try:
        tz: tzinfo = ZoneInfo(get_settings().timezone)
    except ZoneInfoNotFoundError:  # misconfigured/unknown TIMEZONE → fall back to UTC
        tz = timezone.utc
    return datetime.now(tz).date()


def format_size(num_bytes: int | None, *, unknown: str = "?") -> str:
    """Human-readable size, e.g. 245.1 MB.

    ``unknown`` is what a caller with no number gets, in the caller's own terms:
    the worker passes the word in the user's language, while a command-line script
    keeps the neutral default.
    """
    if not num_bytes:
        return unknown
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Strip characters that are illegal in filenames / unsafe for Telegram."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return (cleaned[:max_len] or "media").rstrip(".")


def escape_html(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return html.escape(text, quote=False)
