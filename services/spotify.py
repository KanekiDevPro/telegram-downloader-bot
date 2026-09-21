"""Spotify links are rewritten, not downloaded — because neither engine can fetch them.

Both facts here were measured against the real engines, not assumed:

* yt-dlp answers ``open.spotify.com`` from its ``KnownDRMIE`` policy list — "The
  requested site is known to use DRM protection. It will NOT be supported." No
  cookie, PO token or proxy changes that: it is a list, not a failure.
* the embedded Cobalt instance (v10) has no Spotify service at all. Its supported
  services are bilibili…vk, and its services directory has no ``spotify.js``, so a
  Spotify link comes back as ``error.api.link.unsupported``. ("Cobalt maps Spotify
  through YouTube Music" is a thing cobalt does *not* do in this generation — which
  is why the mapping lives here instead of in the fallback call.)

What *is* reachable is the same song on YouTube, and Spotify publishes what is
needed to find it: the public embed page carries the title, the artists and the
duration of a track with no login and no API key. The search that turns those into
a video then runs on the hardened path this bot already has — cookies, PO tokens,
proxy — with Cobalt still standing behind it as the block fallback, because by then
the link *is* a YouTube one.

So this module is deliberately small: read a Spotify link into a
:class:`SpotifyTrack`, pick the YouTube hit whose length matches it, done. The worker
keeps the user's own URL for the cache key and the record, so a Spotify link that
was mapped once comes back instantly the second time.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import aiohttp

from services.extractor import ExtractionError, ExtractorService, SearchHit, url_host

logger = logging.getLogger(__name__)

#: Hosts that mean "a Spotify link". ``spotify.link`` is the mobile app's share
#: domain: it redirects to ``open.spotify.com``, so the real URL is read from that
#: hop before anything else happens.
SPOTIFY_HOSTS: tuple[str, ...] = ("spotify.com", "spotify.link")

_EMBED_URL = "https://open.spotify.com/embed/track/{track_id}"
#: Spotify's own page is a Next.js app: the track lives in one JSON blob in the
#: document. Parsed as text rather than as HTML — no parser dependency for one
#: script tag (the same reasoning as the cookie-jar code).
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
#: A track id is 22 base62 characters. The locale prefix (``/intl-de/``) and any
#: ``?si=`` share tracking are irrelevant to it.
_TRACK_ID = re.compile(r"/track/([A-Za-z0-9]{22})")

#: How long Spotify's own metadata may take. This is spent *before* the user's
#: download starts, so it is a wait, not a background nicety — long enough for a
#: slow CDN, short enough that a dead Spotify does not hold a worker slot.
LOOKUP_TIMEOUT_S = 20.0

#: How far a candidate's length may drift from Spotify's before it is refused as a
#: different recording. Songs match to the second; a music video carries an intro
#: and an outro, a remaster can be a few seconds off — so the window is generous.
#: It separates "same song, video version" from "wrong track, podcast, 10-hour
#: loop", which is all it is for: the search itself already ranked by relevance.
MAX_DURATION_DRIFT_S = 90


def is_spotify_url(url: str) -> bool:
    """Whether this link is a Spotify one (subdomains included)."""
    host = url_host(url)
    return any(host == known or host.endswith(f".{known}") for known in SPOTIFY_HOSTS)


def track_id(url: str) -> Optional[str]:
    """The Spotify track id in a link, or ``None`` for albums/playlists/artists.

    Only a *track* is rewritten here: an album is a list of songs, and a bot that
    silently downloaded the first one would be answering a question nobody asked.
    """
    match = _TRACK_ID.search(urlparse(url).path)
    return match.group(1) if match else None


@dataclass(frozen=True)
class SpotifyTrack:
    """A track's public metadata — all of it from Spotify, none of it guessed."""

    track_id: str
    title: str
    artists: tuple[str, ...]
    duration_s: Optional[int]

    @property
    def credit(self) -> str:
        """``Artist — Title``, the way a person would say it."""
        return f"{', '.join(self.artists)} — {self.title}" if self.artists else self.title

    @property
    def search_query(self) -> str:
        """What to ask YouTube for: the artists first.

        Order matters: the title alone ranks covers, lyric videos and karaoke
        tracks just as highly, and the artist is what picks the real one out.
        """
        return " ".join([*self.artists, self.title]).strip()


@dataclass(frozen=True)
class SpotifyTarget:
    """A Spotify link, its track, and the YouTube video that stands in for it."""

    url: str
    track: SpotifyTrack
    hit: SearchHit


# ---------------------------------------------------------------------------
# Reading Spotify
# ---------------------------------------------------------------------------


def _lookup_failed(message: str) -> ExtractionError:
    return ExtractionError("SPOTIFY_LOOKUP_FAILED", message)


def _read_track(html: str, track_id_: str) -> SpotifyTrack:
    """Pull the track out of an embed page, or say why that was impossible."""
    match = _NEXT_DATA.search(html)
    if not match:
        raise _lookup_failed(
            "اطلاعات این آهنگ از اسپاتیفای خوانده نشد (صفحهٔ embed شکل همیشگی را نداشت)."
        )
    try:
        entity: Any = json.loads(match.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise _lookup_failed(
            f"اطلاعات این آهنگ از اسپاتیفای خوانده نشد ({exc.__class__.__name__})."
        ) from exc
    if not isinstance(entity, dict):
        raise _lookup_failed("اطلاعات این آهنگ از اسپاتیفای خوانده نشد (شکل داده عوض شده).")

    title = str(entity.get("title") or entity.get("name") or "").strip()
    artists = tuple(
        str(artist["name"]).strip()
        for artist in entity.get("artists") or []
        if isinstance(artist, dict) and artist.get("name")
    )
    duration_ms = entity.get("duration")
    duration_s = (
        int(duration_ms / 1000)
        if isinstance(duration_ms, (int, float)) and duration_ms > 0
        else None
    )
    if not title:
        raise _lookup_failed("عنوان این آهنگ در پاسخ اسپاتیفای نبود.")
    return SpotifyTrack(
        track_id=track_id_, title=title, artists=artists, duration_s=duration_s
    )


async def _get(session: aiohttp.ClientSession, url: str, timeout: float) -> str:
    """Fetch a page as text — with Spotify's own timeout, and its own errors."""
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                raise _lookup_failed(
                    f"اسپاتیفای پاسخ {response.status} داد؛ کمی بعد دوباره بفرست."
                )
            return await response.text()
    except TimeoutError:  # asyncio's, which aiohttp's own timeouts inherit from
        raise _lookup_failed("خواندن اطلاعات اسپاتیفای طول کشید؛ دوباره بفرست.") from None
    except aiohttp.ClientError as exc:
        raise _lookup_failed(f"ارتباط با اسپاتیفای برقرار نشد ({exc.__class__.__name__}).") from exc


async def _canonical(session: aiohttp.ClientSession, url: str, timeout: float) -> str:
    """The share link's real destination (``spotify.link`` → ``open.spotify.com``)."""
    if track_id(url):
        return url
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
        ) as response:
            return str(response.url)
    except aiohttp.ClientError as exc:
        raise _lookup_failed(f"بازکردن لینک کوتاه اسپاتیفای ممکن نشد ({exc.__class__.__name__}).") from exc


async def lookup(url: str, *, timeout: float = LOOKUP_TIMEOUT_S) -> SpotifyTrack:
    """Read a track's public metadata. No login, no key, no cookies.

    A session per lookup on purpose: this runs once per Spotify link (the result is
    cached as a download afterwards), so a long-lived client would be lifecycle to
    manage for a request that happens rarely.
    """
    async with aiohttp.ClientSession() as session:
        real = await _canonical(session, url, timeout)
        found = track_id(real)
        if found is None:
            raise ExtractionError(
                "SPOTIFY_NOT_A_TRACK",
                "از اسپاتیفای فقط لینک یک آهنگ (Track) پشتیبانی می‌شود؛ آلبوم و پلی‌لیست نه.",
            )
        html = await _get(session, _EMBED_URL.format(track_id=found), timeout)
    return _read_track(html, found)


# ---------------------------------------------------------------------------
# Choosing the YouTube stand-in
# ---------------------------------------------------------------------------


def best_hit(track: SpotifyTrack, hits: list[SearchHit]) -> Optional[SearchHit]:
    """The candidate whose length is closest to the track's, or ``None`` if absurd.

    Length is the only signal available for free, and it is a good one: the search
    already ranked by relevance (the query *is* the artist and the title), so this
    exists to catch what relevance cannot — a podcast episode, a full-album upload,
    a cover that runs twice as long. When nobody reports a length, the ranking is
    trusted as it is; refusing the only candidate we have would help no one.
    """
    if not hits:
        return None
    if track.duration_s is None:
        return hits[0]
    measured = [(abs(hit.duration_s - track.duration_s), hit) for hit in hits if hit.duration_s]
    if not measured:
        return hits[0]
    drift, hit = min(measured, key=lambda pair: pair[0])
    if drift > MAX_DURATION_DRIFT_S:
        logger.warning(
            "no YouTube match close enough for %r: nearest is %ss off (%s)",
            track.credit,
            drift,
            hit.url,
        )
        return None
    logger.info(
        "matched %r to %r (%ss vs %ss)", track.credit, hit.title, hit.duration_s, track.duration_s
    )
    return hit


async def youtube_target(url: str, extractor: ExtractorService, *, limit: int = 5) -> SpotifyTarget:
    """The YouTube video that should stand in for this Spotify link.

    Raises :class:`~services.extractor.ExtractionError` with a Spotify-specific code
    when there is no honest answer — and lets a *block* from the search itself
    through untouched: that one is about the YouTube path (and its jar), which is
    exactly what the user's message, the admin alert and the digest are for.
    """
    track = await lookup(url)
    hits = await extractor.search(track.search_query, limit=limit)
    hit = best_hit(track, hits)
    if hit is None:
        raise ExtractionError(
            "SPOTIFY_NO_MATCH",
            "نسخهٔ یوتیوب این آهنگ پیدا نشد. (خودِ اسپاتیفای هم به خاطر DRM قابل دانلود نیست.)",
        )
    logger.info("rewrote spotify link %s (%s) to %s", track.track_id, track.credit, hit.url)
    return SpotifyTarget(url=hit.url, track=track, hit=hit)
