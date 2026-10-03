"""Song lookup under Instagram/TikTok videos (session B2).

A video sometimes carries a song worth fetching on its own. This module names
that song — and only that: it never downloads, never queues, never touches the
cache schema. Delivery asks it for a button, the tap resolves the name through
the normal intake, and everything in between lives in Redis under ``shz:*``.

Providers are small and instance-based. ``metadata`` is a pure function over
the extraction info the engines already produced (it sends nothing anywhere);
``shazamio`` (session B3, opt-in) listens to the audio itself. Unknown names
are skipped with one startup warning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core.i18n import t
from core.utils import canonical_url, sha256_hex
from services import telemetry

logger = logging.getLogger(__name__)

#: The only platforms that ever get the button (``MediaInfo.platform`` form).
SONG_PLATFORMS: tuple[str, ...] = ("instagram", "tiktok")
#: Provider names this build understands. ``shazamio`` resolves only after B3
#: lands; naming it before then is enabling nothing (see ``parse_providers``).
RECOGNIZED_PROVIDERS: tuple[str, ...] = ("metadata", "shazamio")
#: Callback prefix: ``shz:<16-hex digest>`` names a stored mapping, and
#: ``shz:go:<16-hex digest>`` a stored download candidate. Both stay far
#: inside Telegram's 64-byte callback budget.
SONG_CALLBACK_PREFIX = "shz:"
SONG_GO_PREFIX = "shz:go:"
#: The Redis home of a tap mapping: identity + canonical URL as JSON, an int
#: EX away from expiring. Same song link, same key — no growth per tap.
SONG_KEY_PREFIX = "shz:"
SONG_MAPPING_TTL_S = 86400
#: Per-user recognition budget: INCR + int EXPIRE, about 5 per 10 minutes.
SONG_RATE_PREFIX = "shz:rl:"
SONG_COOLDOWN_MAX = 5
SONG_COOLDOWN_WINDOW_S = 600
#: A tap that found nothing leaves a short marker so the next tap does not
#: repeat the work (int EX, about 10 minutes).
SONG_NEG_PREFIX = "shz:neg:"
SONG_NEGATIVE_TTL_S = 600
#: Recognition and sampling never run more than this many at once, per process.
SONG_MAX_CONCURRENCY = 2
#: Circuit breaker (B3 recognizers): this many consecutive failures open the
#: provider for this long. While open the button is hidden (rule 9).
BREAKER_FAILURES = 3
BREAKER_OPEN_S = 600
#: The operator page for a degraded lookup lives in ``bot_state`` under this
#: prefix, throttled to at most one notice per cooldown.
SONG_NOTICE_KEY_PREFIX = "song_id:notice:"
SONG_NOTICE_COOLDOWN_S = 3600

#: Values that name the absence of a song, not a song. Compared case-folded
#: after stripping; matched whole or as the whole parenthetical.
_NO_SONG_VALUES = frozenset(
    {
        "",
        "unknown",
        "untitled",
        "no title",
        "n/a",
        "original sound",
        "original audio",
        "originalsound",
        "sonido original",
        "som original",
        "son original",
        "suono originale",
        "tiktok",
        "instagram",
        "reels",
        "video",
    }
)

#: ``Artist - Title`` with an en/em dash or hyphen, breathing room on both
#: sides. A bare ``Artist-Title`` is a hashtag or a filename until proven
#: otherwise — doubt returns None.
_TITLE_SPLIT_RE = re.compile(r"\s+[—–-]\s+")
#: Leading musical-notation clutter some captions carry.
_TITLE_NOISE_RE = re.compile(r"^[\s♪♫🎵🎶#]+")
#: The audio tier the confirming tap preselects (the menu's own MP3 row).
SONG_AUDIO_QUALITY = "mp3.best"


@dataclass(frozen=True)
class SongIdentity:
    """A named song and where the name came from."""

    artist: str
    title: str
    source: str


def parse_providers(raw: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split ``SHAZAM_PROVIDERS`` into (recognized, unknown), in order.

    Pure: the one startup warning for unknown names is the caller's job (see
    :class:`SongLookupService`), so a probe that parses twice warns once.
    """
    names = [part.strip().lower() for part in str(raw or "").split(",")]
    known = tuple(name for name in names if name in RECOGNIZED_PROVIDERS)
    unknown = tuple(name for name in names if name and name not in RECOGNIZED_PROVIDERS)
    return known, unknown


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _is_junk(value: str) -> bool:
    text = _clean(value).casefold()
    if not text or text in _NO_SONG_VALUES:
        return True
    if "#" in text or "http://" in text or "https://" in text:
        return True
    return False


def _field_of(info: Any, name: str) -> Any:
    if isinstance(info, Mapping):
        return info.get(name)
    return getattr(info, name, None)


def _first_artist(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        for entry in value:
            cleaned = _clean(entry.get("name") if isinstance(entry, Mapping) else entry)
            if cleaned:
                return cleaned
        return ""
    return _clean(value)


def identify_from_metadata(info: Any) -> SongIdentity | None:
    """Name the song from the extraction info, or None when in doubt.

    Pure and conservative: explicit ``track``/``artist`` metadata wins, else a
    short ``Artist - Title`` caption is read — and anything that smells like a
    sentence, a hashtag pile, an "original sound" or the creator's own name
    answers None. Never raises on odd shapes; odd shapes are doubt.
    """
    if info is None:
        return None
    try:
        return _identify(info)
    except Exception:  # noqa: BLE001 — doubt is None, never a traceback
        logger.debug("song metadata unreadable — treating it as no song", exc_info=True)
        return None


def _identify(info: Any) -> SongIdentity | None:
    creator = _clean(_field_of(info, "creator") or _field_of(info, "uploader") or "")
    # A music object (TikTok-style): its own title/author pair.
    music = _field_of(info, "music")
    if isinstance(music, Mapping):
        track = _clean(music.get("title") or music.get("track") or "")
        artist = _first_artist(music.get("author") or music.get("artist") or music.get("uploader"))
        identity = _explicit(track, artist, creator)
        if identity is not None:
            return identity
    track = _clean(_field_of(info, "track") or "")
    artist = _first_artist(_field_of(info, "artist") or _field_of(info, "artists") or "")
    if track or artist:
        return _explicit(track, artist, creator)
    return _from_title(_clean(_field_of(info, "title") or ""), creator)


def _explicit(track: str, artist: str, creator: str) -> SongIdentity | None:
    if not track or not artist or _is_junk(track) or _is_junk(artist):
        return None
    if len(track) > 120 or len(artist) > 120:
        return None
    return SongIdentity(artist=artist, title=track, source="metadata")


def _from_title(title: str, creator: str) -> SongIdentity | None:
    if not title:
        return None
    first_line = title.splitlines()[0] if title.splitlines() else ""
    text = _TITLE_NOISE_RE.sub("", _clean(first_line))
    if not text or len(text) > 100 or _is_junk(text):
        return None
    parts = _TITLE_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) != 2:
        return None
    artist, track = _clean(parts[0]), _clean(parts[1])
    if not (2 <= len(artist) <= 60 and 2 <= len(track) <= 60):
        return None
    if _is_junk(artist) or _is_junk(track):
        return None
    if "@" in artist or "@" in track:
        return None
    if creator and (
        artist.casefold() == creator.casefold() or track.casefold() == creator.casefold()
    ):
        # The creator's name alone is not a song.
        return None
    return SongIdentity(artist=artist, title=track, source="metadata")


def row_text(row: Any, name: str) -> str:
    """One tolerant read from a cache row (Record, mapping or bare object)."""
    try:
        if isinstance(row, Mapping):
            return str(row.get(name) or "")
        return str(row[name] or "")
    except Exception:  # noqa: BLE001 — test doubles and odd rows read as empty
        try:
            return str(getattr(row, name, None) or "")
        except Exception:  # noqa: BLE001 — never let a caption read raise
            return ""


def mapping_digest(canonical: str, artist: str, title: str) -> str:
    """The deterministic ``shz:<digest>`` for one song link."""
    return sha256_hex(f"{canonical}|{artist}|{title}")[:16]


class SongLookupService:
    """The button, the mapping and the manners around them.

    All mutable state lives here (plus Redis): the concurrency semaphore, the
    per-provider breaker streaks and the admin-notice throttle. One instance
    per process, shared by the gateway and the workers.
    """

    def __init__(
        self,
        *,
        redis: Any = None,
        pool: Any = None,
        bot: Any = None,
        admin_ids: Sequence[int] = (),
        providers: str | Sequence[str] = "metadata",
        cooldown_max: int = SONG_COOLDOWN_MAX,
        cooldown_window_s: int = SONG_COOLDOWN_WINDOW_S,
        negative_ttl_s: int = SONG_NEGATIVE_TTL_S,
        max_concurrency: int = SONG_MAX_CONCURRENCY,
        clock: Callable[[], float] | None = None,
    ) -> None:
        raw = ",".join(providers) if isinstance(providers, (list, tuple)) else str(providers or "")
        known, unknown = parse_providers(raw)
        if unknown:
            logger.warning(
                "unknown song providers skipped: %s (recognized: %s)",
                ", ".join(unknown),
                ", ".join(RECOGNIZED_PROVIDERS),
            )
        self._providers = known or ("metadata",)
        self._redis = redis
        self._pool = pool
        self._bot = bot
        self._admin_ids = list(admin_ids or [])
        self._cooldown_max = int(cooldown_max)
        self._cooldown_window_s = int(cooldown_window_s)
        self._negative_ttl_s = int(negative_ttl_s)
        self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        self._clock: Callable[[], float] = clock or time.monotonic
        #: Consecutive recognizer failures per provider (breaker streaks).
        self._failures: dict[str, int] = {}
        #: Monotonic instant a provider's breaker closes again.
        self._opened_until: dict[str, float] = {}
        #: Last admin notice per key (process-level throttle; ``bot_state``
        #: covers restarts and replicas, mirroring force-join).
        self._last_notice: dict[str, float] = {}

    @property
    def providers(self) -> tuple[str, ...]:
        return self._providers

    def slot(self) -> asyncio.Semaphore:
        """The concurrency slot recognizer calls and ffmpeg work run inside."""
        return self._sem

    # ------------------------------------------------------------------
    # Provider health (the B3 circuit breaker lives here)
    # ------------------------------------------------------------------

    def is_healthy(self, name: str) -> bool:
        """Whether a non-metadata recognizer may currently be offered."""
        if name == "metadata":
            return True
        return self._clock() >= self._opened_until.get(name, 0.0)

    def recognizer_available(self) -> bool:
        """Whether any enabled non-metadata recognizer is currently healthy."""
        return any(
            name != "metadata" and self.is_healthy(name) for name in self._providers
        )

    def note_success(self, name: str) -> None:
        self._failures[name] = 0
        self._opened_until.pop(name, None)

    def note_failure(self, name: str) -> bool:
        """Record a failure; True when this failure just opened the breaker."""
        streak = self._failures.get(name, 0) + 1
        self._failures[name] = streak
        if streak >= BREAKER_FAILURES and name not in self._opened_until:
            self._opened_until[name] = self._clock() + BREAKER_OPEN_S
            return True
        return False

    # ------------------------------------------------------------------
    # Mapping: digest -> {artist, title, url, candidate?}
    # ------------------------------------------------------------------

    async def store_mapping(
        self, *, artist: str, title: str, url: str, candidate: str = ""
    ) -> str | None:
        """Remember a tap mapping; None when Redis cannot (no button then)."""
        redis = self._redis
        if redis is None:
            logger.warning("song lookup mapping unwritable (no redis) — hiding the button")
            return None
        canonical = canonical_url(url)
        digest = mapping_digest(canonical, _clean(artist), _clean(title))
        payload = json.dumps(
            {
                "artist": _clean(artist),
                "title": _clean(title),
                "url": canonical,
                "candidate": _clean(candidate),
            }
        )
        try:
            # int() on purpose: redis-py raises DataError on a float EX, and
            # a hand-rolled fake would never catch it (see guardfix).
            await redis.set(f"{SONG_KEY_PREFIX}{digest}", payload, ex=int(SONG_MAPPING_TTL_S))
        except Exception as exc:  # noqa: BLE001 — best-effort cache, never fatal
            logger.warning(
                "song lookup mapping unwritable (%s) — hiding the button",
                type(exc).__name__,
            )
            return None
        return digest

    async def read_mapping(self, digest: str) -> dict[str, Any] | None:
        """The stored mapping, or None when expired, unknown or unreadable."""
        redis = self._redis
        if redis is None or not re.fullmatch(r"[0-9a-f]{16}", digest or ""):
            return None
        try:
            raw = await redis.get(f"{SONG_KEY_PREFIX}{digest}")
        except Exception:  # noqa: BLE001 — a dead cache reads as expired
            logger.debug("song lookup mapping unreadable — treating it as expired", exc_info=True)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    # ------------------------------------------------------------------
    # Limits: per-user cooldown, brief negative cache
    # ------------------------------------------------------------------

    async def check_cooldown(self, user_id: int) -> bool:
        """True when this user may recognize now (fail-open on cache trouble)."""
        redis = self._redis
        if redis is None:
            return True
        key = f"{SONG_RATE_PREFIX}{int(user_id)}"
        try:
            count = await redis.incr(key)
        except Exception:  # noqa: BLE001 — a dead counter never blocks a tap
            logger.debug("song lookup counter unreadable — failing open", exc_info=True)
            return True
        try:
            if int(count) == 1:
                await redis.expire(key, int(self._cooldown_window_s))
        except Exception:  # noqa: BLE001 — the window is a nicety, not the gate
            logger.debug("song lookup rate window unset — continuing", exc_info=True)
        return int(count) <= int(self._cooldown_max)

    async def remember_negative(self, digest: str) -> None:
        redis = self._redis
        if redis is None:
            return
        try:
            await redis.set(
                f"{SONG_NEG_PREFIX}{digest}", "1", ex=int(self._negative_ttl_s)
            )
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            logger.debug("song lookup negative cache unwritable — continuing", exc_info=True)

    async def is_negative(self, digest: str) -> bool:
        redis = self._redis
        if redis is None:
            return False
        try:
            return await redis.get(f"{SONG_NEG_PREFIX}{digest}") is not None
        except Exception:  # noqa: BLE001 — a dead cache means ask again
            logger.debug("song lookup negative cache unreadable — continuing", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Button
    # ------------------------------------------------------------------

    async def button_for_delivery(
        self, *, platform: str, title: str, url: str, lang: str
    ) -> InlineKeyboardMarkup | None:
        """The «find full song» button, or None when it could not work.

        Shown only under Instagram/TikTok videos, and only when the song is
        already named (metadata) or a healthy recognizer could name it at tap
        time. The mapping write and the button are one decision: no stored
        identity, no button (rule 9); a Redis blip hides it with one warning.
        """
        if (platform or "").lower() not in SONG_PLATFORMS:
            return None
        identity = identify_from_metadata({"title": title or ""})
        if identity is None and not self.recognizer_available():
            return None
        digest = await self.store_mapping(
            artist=identity.artist if identity else "",
            title=identity.title if identity else "",
            url=url,
        )
        if digest is None:
            return None
        builder = InlineKeyboardBuilder()
        builder.button(
            text=t("shz.find_button", lang),
            callback_data=f"{SONG_CALLBACK_PREFIX}{digest}",
        )
        return builder.as_markup()

    async def button_for_row(self, row: Any, url: str, lang: str) -> InlineKeyboardMarkup | None:
        """The button for a cached replay — the same state a fresh send gets.

        The row carries what the fresh delivery knew (platform, title); the
        digest is deterministic, so the replay re-stores the very mapping the
        first send wrote. Non-video rows never get it. Never raises.
        """
        try:
            kind = row_text(row, "kind").strip()
            if kind and kind != "video":
                return None
            return await self.button_for_delivery(
                platform=row_text(row, "platform"),
                title=row_text(row, "title"),
                url=url,
                lang=lang,
            )
        except Exception:  # noqa: BLE001 — a replay never fails over a button
            logger.warning(
                "song replay button failed for %s", log_url_hint(url), exc_info=True
            )
            return None

    # ------------------------------------------------------------------
    # Degradation: one warning plus one throttled operator notice
    # ------------------------------------------------------------------

    async def degraded(self, key: str, reason: str) -> None:
        """Page the operator about a lookup outage, throttled like force-join."""
        logger.warning("song lookup degraded for %s (%s)", key, reason)
        if self._pool is None or self._bot is None or not self._admin_ids:
            return
        if not await self._notice_due(key):
            return
        text = f"Song lookup degraded for {key}: {reason}"
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text)
            except Exception:  # noqa: BLE001 — a notice must never break delivery
                logger.warning("could not send a song-lookup notice to %s", admin_id)
        await self._stamp_notice(key)

    async def _notice_due(self, key: str) -> bool:
        now = self._clock()
        last_local = self._last_notice.get(key)
        if last_local is not None and now - last_local < SONG_NOTICE_COOLDOWN_S:
            return False
        try:
            last = await database.get_state(self._pool, f"{SONG_NOTICE_KEY_PREFIX}{key}")
        except Exception:  # noqa: BLE001 — the instance dict still guards us
            logger.debug("song lookup throttle unreadable — notifying", exc_info=True)
            return True
        if last is None:
            return True
        try:
            from datetime import datetime, timezone

            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(last)
            ).total_seconds()
        except ValueError:
            logger.warning(
                "bot_state[song_id:notice:%s] is not a timestamp — treating it as due",
                key,
            )
            return True
        return age >= SONG_NOTICE_COOLDOWN_S

    async def _stamp_notice(self, key: str) -> None:
        self._last_notice[key] = self._clock()
        try:
            from datetime import datetime, timezone

            await database.set_state(
                self._pool,
                f"{SONG_NOTICE_KEY_PREFIX}{key}",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception:  # noqa: BLE001 — the stamp is a nicety, not the page
            logger.debug("could not record a song-lookup notice", exc_info=True)


def search_query(artist: str, title: str) -> str:
    """The full-track query for the existing search facility."""
    return f"{_clean(artist)} - {_clean(title)}"


def log_url_hint(url: str) -> str:
    """The redacted URL form this module's warnings use (never raw)."""
    return telemetry.log_url(url)
