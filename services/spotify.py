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

Reading the track is also where the *presentation* comes from. The users' complaint
about this route was that it announced itself ("mapped to YouTube") and then handed
back a YouTube file with a YouTube caption. So the same public page supplies what
the delivered file needs to look like the song it is: title, artists, release year,
and the cover art (``visualIdentity.image``, rewritten onto Spotify's canonical
image host because the embed's own URLs are locale-specific). The album is the one
field the embed payload does **not** carry — it comes from the track page's
``og:description``, best-effort, and its absence costs a line of caption rather
than the download. ``download_cover`` fetches the artwork for Telegram's audio
thumbnail; the tagging itself happens at the upload, where Telegram stores title,
performer and cover in the file — nothing is re-encoded to add metadata.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from html import unescape as _unescape
from pathlib import Path
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
#: The track *page* (the one a browser shows — the embed URL without ``/embed``).
#: It is the only public place the **album** appears: the embed payload carries the
#: title, the artists, the length and the cover, and no album at all (measured).
#: Derived rather than written out so a test that points ``_EMBED_URL`` at a local
#: origin takes this request along with it.
_PAGE_PATH = "/track/"
#: The document's own summary line: ``Artist · Album · Song · 1987``. The middle
#: part is the album; the trailing year is what tells us the shape is the one we
#: recognise (the word before it is localised, so it cannot be matched on).
_OG_DESCRIPTION = re.compile(r'<meta property="og:description" content="([^"]*)"')
_ARTIST_SEPARATOR = "·"
#: The image CDN's canonical host. The embed's own image URLs are locale-specific
#: (``image-cdn-fa.spotifycdn.com``) while the image id is the same everywhere, and
#: the 640px URL Spotify itself puts in ``og:image`` uses the canonical host.
_IMAGE_CDN = "https://i.scdn.co/image/{image_id}"
_IMAGE_ID = re.compile(r"/image/([A-Za-z0-9]+)")
#: A track id is 22 base62 characters. The locale prefix (``/intl-de/``) and any
#: ``?si=`` share tracking are irrelevant to it.
_TRACK_ID = re.compile(r"/track/([A-Za-z0-9]{22})")

#: What Telegram accepts as an audio thumbnail, and what is worth sending: the
#: 300px cover is the sweet spot between "too small to see" and "refused".
_THUMBNAIL_MAX_PX = 320
_THUMBNAIL_MIN_PX = 200
#: Telegram's own ceiling is 200 kB; leave a little room, because a slightly larger
#: file means the whole upload is refused rather than the thumbnail dropped.
MAX_COVER_BYTES = 190_000
#: Where the artwork is written inside a job directory (cleaned up with the job).
COVER_FILE_NAME = "cover.jpg"

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
    """A track's public metadata — all of it from Spotify, none of it guessed.

    ``album`` and ``covers`` are what make the delivered file a *Spotify* track
    rather than a YouTube rip: the album is the one field Spotify publishes only on
    the track page (so it can be absent), and the covers are the frame Telegram is
    handed as the audio thumbnail. Everything here is optional except the identity,
    because a missing cover must never cost a user their download.
    """

    track_id: str
    title: str
    artists: tuple[str, ...]
    duration_s: Optional[int]
    album: str = ""
    year: Optional[int] = None
    #: ``(width, url)`` for every size Spotify published, widest last.
    covers: tuple[tuple[int, str], ...] = ()

    @property
    def credit(self) -> str:
        """``Artist — Title``, the way a person would say it."""
        return f"{', '.join(self.artists)} — {self.title}" if self.artists else self.title

    @property
    def artist(self) -> str:
        """Every artist in one line (what ``send_audio`` calls the performer)."""
        return ", ".join(self.artists)

    @property
    def thumbnail_url(self) -> str:
        """A cover small enough for Telegram's thumbnail rules (``""`` if none).

        Telegram refuses a thumbnail wider than 320px or heavier than 200 kB, so a
        640px square — the largest Spotify publishes — is not one: the 300px size
        is, and it is the one a chat client shows anyway. A track whose only covers
        are too large travels with its title and artist and no artwork.
        """
        small = sorted((width, url) for width, url in self.covers if width <= _THUMBNAIL_MAX_PX)
        if not small:
            return ""
        for width, url in small:
            if width >= _THUMBNAIL_MIN_PX:
                return url
        return small[-1][1]

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


def page_url(track_id: str) -> str:
    """The browser page for a track (the embed URL without ``/embed``)."""
    base = _EMBED_URL.replace("/embed" + _PAGE_PATH, _PAGE_PATH, 1)
    return base.format(track_id=track_id)


def _canonical_image(url: str) -> str:
    """Rewrite a locale-specific image URL onto Spotify's canonical image host."""
    match = _IMAGE_ID.search(url)
    return _IMAGE_CDN.format(image_id=match.group(1)) if match else url


def _covers(visual: Any) -> tuple[tuple[int, str], ...]:
    """Every cover Spotify published for this track, as ``(width, url)``.

    The payload nests them under ``visualIdentity.image``; the widths are what a
    caller needs, because which one is usable is a question about limits.
    """
    images = visual.get("image") if isinstance(visual, dict) else None
    found: list[tuple[int, str]] = []
    for image in images if isinstance(images, list) else ():
        if not isinstance(image, dict):
            continue
        url = str(image.get("url") or "").strip()
        raw_width = image.get("maxWidth") or image.get("width") or 0
        width = int(raw_width) if isinstance(raw_width, (int, float)) else 0
        if url:
            found.append((width, _canonical_image(url)))
    return tuple(sorted(found))


def _year(release: Any) -> Optional[int]:
    """The release year out of ``releaseDate.isoString`` (``None`` when absent)."""
    iso = release.get("isoString") if isinstance(release, dict) else None
    match = re.match(r"(\d{4})", str(iso or ""))
    return int(match.group(1)) if match else None


def album_from_page(html: str) -> str:
    """The album name in a track page's ``og:description`` (``""`` when unclear).

    The line Spotify writes there is ``Artist · Album · Song · 1987`` — the label
    before the year is localised, so it is not matched on; the *shape* is what is
    checked instead (four parts, a year last), and anything else yields no album
    rather than a wrong one. This is the only public source of the album: the embed
    payload does not carry it at all.
    """
    match = _OG_DESCRIPTION.search(html)
    if not match:
        return ""
    parts = [_unescape(part).strip() for part in match.group(1).split(_ARTIST_SEPARATOR)]
    if len(parts) == 4 and parts[3].isdigit():
        return parts[1]
    return ""


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
        track_id=track_id_,
        title=title,
        artists=artists,
        duration_s=duration_s,
        year=_year(entity.get("releaseDate")),
        covers=_covers(entity.get("visualIdentity")),
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


async def _album_name(session: aiohttp.ClientSession, found: str, timeout: float) -> str:
    """The album, read from the track page — and never a reason to fail a link.

    Two round trips instead of one, for one line of metadata: if this request
    fails (an older page layout, a redirect, a network that went away between the
    two calls) the track is still complete enough to download and tag, so the
    failure is logged and swallowed.
    """
    try:
        html = await _get(session, page_url(found), timeout)
    except ExtractionError as exc:
        logger.info("album not readable for %s (%s) — continuing without it", found, exc.code)
        return ""
    return album_from_page(html)


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
        track = _read_track(html, found)
        album = await _album_name(session, found, timeout)
    return replace(track, album=album) if album else track


async def download_cover(
    track: SpotifyTrack, directory: Path, *, timeout: float = LOOKUP_TIMEOUT_S
) -> Optional[Path]:
    """Fetch the track's artwork into ``directory`` for use as an audio thumbnail.

    Returns ``None`` — never raises — when there is no cover, when it is too heavy
    for Telegram's thumbnail rules, or when the fetch simply fails: a song without
    artwork is still the song, and a download must not die over a JPEG.
    """
    url = track.thumbnail_url
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
            ) as response:
                if response.status != 200:
                    logger.info("cover art for %s answered %s", track.track_id, response.status)
                    return None
                data = await response.read()
    except (TimeoutError, aiohttp.ClientError) as exc:
        logger.info("could not fetch the cover art for %s (%r)", track.track_id, exc)
        return None
    if not data or len(data) > MAX_COVER_BYTES:
        logger.info(
            "cover art for %s is %s bytes — skipping it (Telegram's ceiling is 200 kB)",
            track.track_id,
            len(data),
        )
        return None
    try:
        path = directory / COVER_FILE_NAME
        path.write_bytes(data)
    except OSError as exc:
        logger.info("could not write the cover art for %s (%r)", track.track_id, exc)
        return None
    return path


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
