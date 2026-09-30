"""Pluggable audio providers: identity in, normalized candidates out.

A provider discovers candidates, matches tracks and exposes source facts; it
never owns UI, delivery, cache policy, ranking or conversion policy — those
belong to the handlers, the worker and :mod:`services.quality`. Today two
implementations exist: the metadata resolver (identity only, no bytes) and the
media fetcher (normalized candidates from the installed fetcher). A future
legitimate Hi-Fi source registers the same contract and nothing else changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Optional, Protocol, Sequence

from services import spotify
from services.audio_models import AudioCandidate, TrackIdentity
from services.extractor import ExtractionError, ExtractorService, MediaInfo, SearchHit
from services.quality import QualityEngine

logger = logging.getLogger(__name__)


class AudioProvider(Protocol):
    """What every audio source implements — metadata in, candidates out."""

    name: str

    async def search_by_isrc(self, isrc: str) -> list[AudioCandidate]:
        """Candidates carrying this exact recording id (``[]`` when unindexed)."""
        ...

    async def search_by_metadata(self, identity: TrackIdentity) -> list[AudioCandidate]:
        """Candidates matching artist/title/album/duration, scored by signal."""
        ...

    async def resolve(self, candidate: AudioCandidate) -> str:
        """The fetchable URL behind a selected candidate."""
        ...


class ProviderRegistry:
    """Every audio source, by name — the engine reads across all of them.

    Registering is all a future legitimate provider needs: ranking, planning
    and delivery never branch on a name, so no other module changes with it.
    One dead source must not sink the rest, so a provider's failure is logged
    and skipped, never raised through the others.
    """

    def __init__(self) -> None:
        self._providers: dict[str, AudioProvider] = {}

    def register(self, provider: AudioProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Optional[AudioProvider]:
        return self._providers.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    async def candidates(self, identity: TrackIdentity) -> list[AudioCandidate]:
        """ISRC-exact matches first, then every provider's metadata candidates.

        The recording id is the strongest identity signal this layer has, so a
        provider that indexes it answers before any title search runs — and a
        leg that fails (ISRC or metadata alike) is logged and skipped, never
        raised through the others. Duplicates keep their ISRC reading.
        """
        found: list[AudioCandidate] = []
        if identity.isrc:
            for name, provider in self._providers.items():
                try:
                    found.extend(
                        _as_isrc_match(candidate)
                        for candidate in await provider.search_by_isrc(identity.isrc)
                    )
                except Exception:
                    logger.warning(
                        "audio provider %r failed — skipping it", name, exc_info=True
                    )
        seen = {(candidate.provider_name, candidate.provider_track_id) for candidate in found}
        for name, provider in self._providers.items():
            try:
                for candidate in await provider.search_by_metadata(identity):
                    if (candidate.provider_name, candidate.provider_track_id) in seen:
                        continue
                    seen.add((candidate.provider_name, candidate.provider_track_id))
                    found.append(candidate)
            except Exception:
                logger.warning("audio provider %r failed — skipping it", name, exc_info=True)
        return found


#: The identity confidence an exact ISRC match carries: above any metadata
#: score (0.99 is the fetched-evidence ceiling in ``score_hit``), below the
#: platform's own id (1.0). The number says *which recording*, never how good
#: its bytes are — the engine still ranks audio facts underneath it.
ISRC_MATCH_CONFIDENCE = 0.98


def _as_isrc_match(candidate: AudioCandidate) -> AudioCandidate:
    """Stamp the exact-recording signal; the audio facts ride through untouched.

    Only the signal changes (method, and a floor under the confidence) — the
    codec, bitrate, lossless flags and duration stay exactly what the provider
    reported, so a doubtful file can never dress as a verified one.
    """
    if candidate.match_method == "isrc":
        return candidate
    try:
        confidence = float(candidate.match_confidence or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return replace(
        candidate,
        match_method="isrc",
        match_confidence=max(confidence, ISRC_MATCH_CONFIDENCE),
    )


def spotify_track_identity(track: spotify.SpotifyTrack) -> TrackIdentity:
    """The platform's own reading of a track: exact by construction.

    Confidence 1.0 is the id's, not the audio's — it says *which* song, never
    how good any bytes for it are. ISRC rides along when the page carried one.
    """
    return TrackIdentity(
        provider="spotify",
        track_id=track.track_id,
        artist=track.artist,
        title=track.title,
        album=track.album,
        duration_s=track.duration_s,
        year=track.year,
        isrc=track.isrc,
        match_method="spotify-id",
        match_confidence=1.0,
    )


_WORDS = re.compile(r"[a-z0-9]+")


def _title_overlap(a: str, b: str) -> float:
    mine = set(_WORDS.findall(a.lower()))
    theirs = set(_WORDS.findall(b.lower()))
    if not mine:
        return 0.0
    return len(mine & theirs) / len(mine)


def score_hit(identity: TrackIdentity, hit: SearchHit) -> tuple[float, str]:
    """How strongly a fetched hit matches the identity — confidence, and how.

    Duration is the hard signal (same windows the fetcher already trusts);
    title overlap only ever adds. Capped below 1.0: a fetched hit is evidence,
    never the platform's own id.
    """
    if identity.duration_s and hit.duration_s:
        drift = abs(hit.duration_s - identity.duration_s)
        if drift <= 2:
            base = 0.95
        elif drift <= 15:
            base = 0.85
        elif drift <= spotify.MAX_DURATION_DRIFT_S:
            base = 0.60
        else:
            base = 0.25
        method = "duration"
    else:
        base, method = 0.50, "ranking"
    bonus = 0.04 * _title_overlap(
        f"{identity.artist} {identity.title}", hit.title or ""
    )
    if bonus > 0 and method == "duration":
        method = "duration+title"
    elif bonus > 0:
        method = "title"
    return min(0.99, base + bonus), method


class SpotifyMetadataResolver:
    """Identity only: what the song is, never where its bytes live.

    A lookup failure stays a lookup failure (the fetcher's own error codes) —
    metadata is not proof of audio availability, and this class never claims a
    downloadable file.
    """

    name = "spotify-metadata"

    async def resolve_metadata(self, url: str) -> TrackIdentity:
        return spotify_track_identity(await spotify.lookup(url))


class YouTubeProvider:
    """Candidates from the installed fetcher: inspected, never assumed.

    No hard-coded codec or bitrate lives here — the formats yt-dlp actually
    exposes are read per link (see :func:`candidate_from_media_info`), and a
    source that declares nothing verifiable arrives unverified.
    """

    name = "youtube"

    def __init__(self, extractor: ExtractorService, *, limit: int = 5) -> None:
        self._extractor = extractor
        self._limit = limit

    async def search_by_isrc(self, isrc: str) -> list[AudioCandidate]:
        # No recording-id index exists on this source; metadata search is the
        # honest route (callers fall back to it — never to title-only guessing).
        logger.debug("no ISRC index on this source — metadata search instead")
        return []

    async def search_by_metadata(self, identity: TrackIdentity) -> list[AudioCandidate]:
        query = f"{identity.artist} {identity.title}".strip() or identity.title
        if not query:
            return []
        hits = await self._extractor.search(query, limit=self._limit)
        return [self._hit_candidate(identity, hit) for hit in hits]

    def _hit_candidate(self, identity: TrackIdentity, hit: SearchHit) -> AudioCandidate:
        confidence, method = score_hit(identity, hit)
        return AudioCandidate(
            identity=identity,
            provider_name=self.name,
            provider_track_id=hit.url,
            match_method=method,
            match_confidence=confidence,
            duration_s=hit.duration_s,
        )

    async def resolve(self, candidate: AudioCandidate) -> str:
        return candidate.provider_track_id


def candidate_from_media_info(
    identity: TrackIdentity,
    url: str,
    info: MediaInfo,
    *,
    match_method: str = "duration+title",
    match_confidence: float = 0.9,
) -> AudioCandidate:
    """The fetched link's own audio facts, normalized — unknowns kept unknown.

    Only what the extraction reported becomes a fact (container, declared rate);
    the codec is *not* guessed from the container, and nothing here is marked
    verified: this source's bytes are never provider-verified lossless.
    """
    return AudioCandidate(
        identity=identity,
        provider_name=YouTubeProvider.name,
        provider_track_id=url,
        codec=None,
        container=f".{info.audio_ext}" if info.audio_ext else None,
        bitrate_bps=(
            int(info.audio_kbps) * 1000 if info.audio_kbps else None
        ),
        channels=2,
        is_lossless=False,
        provider_verified_lossless=False,
        match_method=match_method,
        match_confidence=match_confidence,
    )


def candidates_from_sequence(
    identity: TrackIdentity, hits: Sequence[SearchHit]
) -> list[AudioCandidate]:
    """Score raw fetched hits without a fetcher — the ranking's offline half."""
    scored: list[AudioCandidate] = []
    for hit in hits:
        confidence, method = score_hit(identity, hit)
        scored.append(
            AudioCandidate(
                identity=identity,
                provider_name=YouTubeProvider.name,
                provider_track_id=hit.url,
                match_method=method,
                match_confidence=confidence,
                duration_s=hit.duration_s,
            )
        )
    return scored


def build_default_registry(
    extractor: ExtractorService, *, limit: int = 5
) -> ProviderRegistry:
    """The production registry: every audio source the bot may serve today.

    One function so the worker and the tests build the same pipeline — a new
    legitimate provider registers here and nowhere else changes for discovery.
    """
    registry = ProviderRegistry()
    registry.register(YouTubeProvider(extractor, limit=limit))
    return registry


async def spotify_audio_target(
    url: str, extractor: ExtractorService, *, limit: int = 5
) -> spotify.SpotifyTarget:
    """The YouTube stand-in for a Spotify link, chosen through the pipeline:

    Spotify metadata → TrackIdentity → ProviderRegistry → QualityEngine →
    resolved URL. The existing extractor stays the only thing that touches the
    network for discovery and download; this layer only decides *which* source
    is the song. A match nobody's length vouches for raises ``SPOTIFY_NO_MATCH``
    (same code and message as the legacy rewrite), and the returned candidate
    is never verified lossless — YouTube bytes make no such promise.
    """
    track = await spotify.lookup(url)
    identity = spotify_track_identity(track)
    registry = build_default_registry(extractor, limit=limit)
    candidates = await registry.candidates(identity)
    if not candidates:
        raise ExtractionError(
            "SPOTIFY_NO_MATCH",
            "نسخهٔ یوتیوب این آهنگ پیدا نشد. (خودِ اسپاتیفای هم به خاطر DRM قابل دانلود نیست.)",
        )
    selected = QualityEngine.select(candidates)
    if (
        identity.duration_s is not None
        and selected.duration_s is not None
        and abs(selected.duration_s - identity.duration_s) > spotify.MAX_DURATION_DRIFT_S
    ):
        logger.warning(
            "no YouTube match close enough for %r: selected is %ss off (%s)",
            track.credit,
            abs(selected.duration_s - identity.duration_s),
            selected.provider_track_id,
        )
        raise ExtractionError(
            "SPOTIFY_NO_MATCH",
            "نسخهٔ یوتیوب این آهنگ پیدا نشد. (خودِ اسپاتیفای هم به خاطر DRM قابل دانلود نیست.)",
        )
    provider = registry.get(selected.provider_name)
    if provider is None:
        raise ExtractionError(
            "SPOTIFY_NO_MATCH",
            "نسخهٔ یوتیوب این آهنگ پیدا نشد. (خودِ اسپاتیفای هم به خاطر DRM قابل دانلود نیست.)",
        )
    resolved_url = await provider.resolve(selected)
    logger.info("rewrote spotify link %s (%s) to %s", track.track_id, track.credit, resolved_url)
    return spotify.SpotifyTarget(
        url=resolved_url,
        track=track,
        hit=SearchHit(
            url=resolved_url,
            title=f"{track.artist} - {track.title}",
            duration_s=selected.duration_s,
        ),
    )
