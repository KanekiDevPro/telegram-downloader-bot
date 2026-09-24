"""Small shared helpers: hashing, URL handling, formatting, safe filenames."""

from __future__ import annotations

import hashlib
import html
import re
from datetime import date, datetime, timezone, tzinfo
from typing import Literal, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.config import get_settings

#: What a user can ask for: the full video, or just the audio track.
MediaFormat = Literal["video", "audio"]

#: How good. A video tier is a *ceiling* (``bestvideo[height<=N]``), never an
#: upscale — asking for 1080p on a 480p video gets the 480p file, which is what
#: "up to 1080p" means on the button. An audio tier names the *container* to
#: produce and, where the codec has a quality knob, the level on it — the
#: mapping from tier to real encoder settings lives in ``services/extractor.py``.
#: ``cast`` in :func:`normalize_quality` accepts any height the ladder allows —
#: the literal lists the *named* tiers; a probed 2160p option is spelled by number.
Quality = Literal[
    "best",
    "1080",
    "720",
    "480",
    "mp3",
    "mp3.best",
    "mp3.high",
    "mp3.small",
    "m4a",
    "m4a.high",
    "m4a.balanced",
    "m4a.small",
    "opus.best",
    "opus.high",
    "opus.balanced",
    "opus.small",
    "wav",
    "flac",
]
VIDEO_QUALITIES: tuple[Quality, ...] = ("best", "1080", "720", "480")

#: The audio containers this bot can genuinely deliver: the post-processor
#: really builds each one (``services/extractor.py``), Telegram plays each as
#: audio, and the fallback engine can be asked for each by name.
AudioFormat = Literal["mp3", "m4a", "flac", "opus", "wav"]
AUDIO_FORMATS: tuple[AudioFormat, ...] = ("mp3", "m4a", "flac", "opus", "wav")

#: The quality presets a format may have, best first. ``wav`` and ``flac`` have
#: none on purpose: raw and lossless output have no bitrate to move, so offering
#: "smaller WAV" would be a button that cannot do what it says.
AudioLevel = Literal["best", "high", "balanced", "small"]
AUDIO_LEVELS: tuple[AudioLevel, ...] = ("best", "high", "balanced", "small")
AUDIO_FORMAT_LEVELS: dict[str, tuple[AudioLevel, ...]] = {
    "mp3": AUDIO_LEVELS,
    "m4a": AUDIO_LEVELS,
    "opus": AUDIO_LEVELS,
    "flac": (),
    "wav": (),
}

#: ``(format, level)`` → the tier name that travels in queues and cache keys.
#: Two spellings predate the presets and are kept as the canonical name of their
#: level — ``mp3`` (the balanced 192k re-encode) and ``m4a`` (the untouched
#: source stream) — because cache rows and queued tasks already own them; a
#: rename would silently orphan every cached file on the next deploy.
AUDIO_TIERS: dict[tuple[str, str], Quality] = {
    ("mp3", "best"): "mp3.best",
    ("mp3", "high"): "mp3.high",
    ("mp3", "balanced"): "mp3",
    ("mp3", "small"): "mp3.small",
    ("m4a", "best"): "m4a",
    ("m4a", "high"): "m4a.high",
    ("m4a", "balanced"): "m4a.balanced",
    ("m4a", "small"): "m4a.small",
    ("opus", "best"): "opus.best",
    ("opus", "high"): "opus.high",
    ("opus", "balanced"): "opus.balanced",
    ("opus", "small"): "opus.small",
}#: Every audio tier, in the order the menus offer them.
AUDIO_QUALITIES: tuple[Quality, ...] = (
    *(
        AUDIO_TIERS[(fmt, level)]
        for fmt in AUDIO_FORMATS
        for level in AUDIO_LEVELS
        if (fmt, level) in AUDIO_TIERS
    ),
    "wav",
    "flac",
)

#: The same level under the alias a button (or a pre-preset queue payload) may
#: carry. Canonical names only ever come out of :func:`normalize_quality`.
_TIER_ALIASES: dict[str, str] = {
    "mp3.balanced": "mp3",
    "m4a.best": "m4a",
    "wav.best": "wav",
    "flac.best": "flac",
}

#: What a request means when the user did not pick a tier: today's behaviour, and
#: therefore also the cache key an existing row already owns.
DEFAULT_VIDEO_QUALITY: Quality = "best"
DEFAULT_AUDIO_QUALITY: Quality = "mp3"


def default_quality(media_format: str) -> Quality:
    """The tier a request falls back to for that media type."""
    return DEFAULT_AUDIO_QUALITY if media_format == "audio" else DEFAULT_VIDEO_QUALITY


def is_video_height(value: object) -> bool:
    """Whether a tier spells a real video height (``"1080"``, ``"2160"``, …).

    The menu is drawn from what a link *actually* has (see
    ``services/extractor.video_options``), so a tier can be a height the static
    table never named. Anything outside the sane ladder is refused — the number
    on a crafted callback is data, and ``[height<=999999]`` is not a request.
    """
    try:
        height = int(str(value).strip())
    except (TypeError, ValueError):
        return False
    return 144 <= height <= 4320


def normalize_quality(value: object, media_format: str = "video") -> Quality:
    """Coerce a tier (a button tap, a queue payload, a DB value) to a valid one.

    An unknown or absent quality is the default for the *format*, never an error:
    an old queued task carries no tier at all, and re-downloading it at "best" is
    exactly what it asked for. Aliases resolve to their canonical spelling so a
    cache key never depends on which button produced the same request.
    """
    text = str(value or "").strip().lower()
    text = _TIER_ALIASES.get(text, text)
    allowed = AUDIO_QUALITIES if media_format == "audio" else VIDEO_QUALITIES
    for candidate in allowed:
        if text == candidate:
            return candidate
    if media_format == "video" and is_video_height(text):
        # A height the probe offered and the static table never named. It is a
        # ceiling like the named ones (``format_selector`` builds ``[height<=N]``
        # from it), so it keeps its own spelling — collapsing it onto "best" would
        # merge two different requests into one cache key.
        return cast(Quality, text)
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


def local_midnight(day: date) -> datetime:
    """The instant a local day starts — a window boundary that is a whole day.

    Same timezone resolution as :func:`today_local` (misconfigured TIMEZONE →
    UTC), so a window built from these never cuts a local day in half.
    """
    try:
        tz: tzinfo = ZoneInfo(get_settings().timezone)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return datetime(day.year, day.month, day.day, tzinfo=tz)


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
