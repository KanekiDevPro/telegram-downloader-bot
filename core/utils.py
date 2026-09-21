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

#: What a user can ask for: the full video, or just the MP3 audio track.
MediaFormat = Literal["video", "audio"]

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


def format_size(num_bytes: int | None) -> str:
    """Human-readable size, e.g. 245.1 MB."""
    if not num_bytes:
        return "نامشخص"
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
