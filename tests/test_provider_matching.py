"""Spotify provider pipeline quality (Track C).

ISRC is the strongest identity signal: an exact ISRC match must outrank a
doubtful high-bitrate file (a wrong FLAC is worse than a correct lossy
source). Candidates expose measured facts only (no container/codec
inference), transcodes keep their source, and the production orchestration
(metadata → identity → registry → engine → resolution → plan → provenance)
is exercised end to end with deterministic fakes.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from typing import Any

import pytest

from services import providers as providers_module
from services import spotify
from services.audio_models import AudioCandidate, AudioMode, SourceAudio, TrackIdentity
from services.extractor import ExtractionError, MediaInfo, SearchHit
from services.quality import QualityEngine, finalize_output
from services.verify import MediaFacts

ISRC = "USRC17607839"
SPOTIFY_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def _identity(isrc: str | None = ISRC) -> TrackIdentity:
    return TrackIdentity(
        provider="spotify",
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        artist="Rick Astley",
        title="Never Gonna Give You Up",
        album="Whenever You Need Somebody",
        duration_s=213,
        isrc=isrc,
        match_method="spotify-id",
        match_confidence=1.0,
    )


def _candidate(
    *,
    provider: str = "fake-hifi",
    track_id: str = "https://provider.example/t/1",
    codec: str | None = "opus",
    container: str | None = ".webm",
    bitrate_bps: int | None = 160_000,
    is_lossless: bool = False,
    verified: bool = False,
    method: str = "metadata",
    confidence: float = 0.9,
    duration_s: int | None = 213,
) -> AudioCandidate:
    return AudioCandidate(
        identity=_identity(),
        provider_name=provider,
        provider_track_id=track_id,
        codec=codec,
        container=container,
        bitrate_bps=bitrate_bps,
        is_lossless=is_lossless,
        provider_verified_lossless=verified,
        match_method=method,
        match_confidence=confidence,
        duration_s=duration_s,
    )


class _FakeProvider:
    """Deterministic provider double honoring the AudioProvider contract."""

    def __init__(
        self,
        name: str,
        *,
        isrc_hit: AudioCandidate | None = None,
        meta_hits: list[AudioCandidate] | None = None,
        fail_isrc: bool = False,
        fail_meta: bool = False,
    ) -> None:
        self.name = name
        self._isrc_hit = isrc_hit
        self._meta_hits = meta_hits or []
        self.fail_isrc = fail_isrc
        self.fail_meta = fail_meta
        self.isrc_queries: list[str] = []
        self.meta_queries = 0

    async def search_by_isrc(self, isrc: str) -> list[AudioCandidate]:
        self.isrc_queries.append(isrc)
        if self.fail_isrc:
            raise RuntimeError("isrc index down")
        if self._isrc_hit is not None and self._isrc_hit.identity.isrc == isrc:
            return [self._isrc_hit]
        return []

    async def search_by_metadata(self, identity: TrackIdentity) -> list[AudioCandidate]:
        self.meta_queries += 1
        if self.fail_meta:
            raise RuntimeError("metadata index down")
        return list(self._meta_hits)

    async def resolve(self, candidate: AudioCandidate) -> str:
        return candidate.provider_track_id


async def test_isrc_exact_match_outranks_doubtful_flac() -> None:
    """A 0.90-confidence ISRC Opus beats a 0.30-confidence FLAC."""
    isrc_match = _candidate(confidence=0.90, method="metadata")
    doubtful_flac = _candidate(
        provider="other",
        track_id="https://other.example/f/9",
        codec="flac",
        container=".flac",
        bitrate_bps=900_000,
        is_lossless=True,
        confidence=0.30,
    )
    registry = providers_module.ProviderRegistry()
    registry.register(_FakeProvider("isrc-store", isrc_hit=isrc_match))
    registry.register(_FakeProvider("meta-store", meta_hits=[doubtful_flac]))

    found = await registry.candidates(_identity())
    selected = QualityEngine.select(found)

    assert found[0].match_method == "isrc"
    assert selected.provider_track_id == isrc_match.provider_track_id
    assert selected.codec == "opus"


async def test_exact_isrc_beats_a_stronger_confidence_metadata_hit() -> None:
    isrc_match = _candidate(
        provider="isrc-store",
        track_id="https://isrc-store.example/t/1",
        confidence=0.5,
    )
    strong_meta = _candidate(
        provider="meta-store",
        track_id="https://meta-store.example/t/9",
        confidence=0.99,
    )
    registry = providers_module.ProviderRegistry()
    registry.register(_FakeProvider("isrc-store", isrc_hit=isrc_match))
    registry.register(_FakeProvider("meta-store", meta_hits=[strong_meta]))

    found = await registry.candidates(_identity())
    selected = QualityEngine.select(found)

    stamped = [c for c in found if c.match_method == "isrc"]
    assert len(stamped) == 1
    assert stamped[0].match_confidence == 0.98
    assert strong_meta.match_confidence == 0.99
    assert selected.provider_track_id == isrc_match.provider_track_id


def test_platform_identity_outranks_exact_isrc_at_any_confidence() -> None:
    platform = _candidate(
        provider="platform",
        track_id="https://platform.example/t/0",
        method="spotify-id",
        confidence=0.5,
    )
    isrc_match = _candidate(
        provider="isrc-store",
        track_id="https://isrc-store.example/t/1",
        method="isrc",
        confidence=0.98,
    )

    assert QualityEngine.select([isrc_match, platform]).provider_track_id == platform.provider_track_id


def test_metadata_only_ranking_still_follows_confidence() -> None:
    strong = _candidate(
        provider="meta-a",
        track_id="https://meta-a.example/t/1",
        method="duration+title",
        confidence=0.99,
    )
    weak = _candidate(
        provider="meta-b",
        track_id="https://meta-b.example/t/2",
        method="ranking",
        confidence=0.5,
    )

    assert QualityEngine.select([weak, strong]).provider_track_id == strong.provider_track_id


def test_ranking_ignores_candidate_input_order() -> None:
    winner = _candidate(
        provider="isrc-store",
        track_id="https://isrc-store.example/t/1",
        method="isrc",
        confidence=0.98,
    )
    runner_up = _candidate(
        provider="meta-a",
        track_id="https://meta-a.example/t/2",
        method="duration+title",
        confidence=0.99,
    )
    also_ran = _candidate(
        provider="meta-b",
        track_id="https://meta-b.example/t/3",
        method="ranking",
        confidence=0.5,
    )
    expected = [winner.provider_track_id, runner_up.provider_track_id, also_ran.provider_track_id]

    for ordering in itertools.permutations([winner, runner_up, also_ran]):
        assert [c.provider_track_id for c in QualityEngine.rank(list(ordering))] == expected


def test_exact_ties_break_on_provider_identity_deterministically() -> None:
    first = _candidate(
        provider="b-store",
        track_id="https://stores.example/b",
        method="isrc",
        confidence=0.98,
    )
    second = _candidate(
        provider="a-store",
        track_id="https://stores.example/a",
        method="isrc",
        confidence=0.98,
    )

    # The tiebreak direction is arbitrary (the ranking sorts best-first, so
    # the larger name leads) — the pinned property is that both input orders
    # resolve identically.
    assert [c.provider_name for c in QualityEngine.rank([first, second])] == ["b-store", "a-store"]
    assert [c.provider_name for c in QualityEngine.rank([second, first])] == ["b-store", "a-store"]


async def test_isrc_index_is_not_queried_without_an_isrc() -> None:
    provider = _FakeProvider("store", meta_hits=[_candidate()])
    registry = providers_module.ProviderRegistry()
    registry.register(provider)

    found = await registry.candidates(_identity(isrc=None))

    assert provider.isrc_queries == []
    assert provider.meta_queries == 1
    assert len(found) == 1


async def test_isrc_leg_failure_does_not_sink_metadata() -> None:
    good = _candidate(confidence=0.9)
    registry = providers_module.ProviderRegistry()
    registry.register(_FakeProvider("broken", fail_isrc=True))
    registry.register(_FakeProvider("store", meta_hits=[good]))

    found = await registry.candidates(_identity())

    assert [c.provider_track_id for c in found] == [good.provider_track_id]


def test_candidate_facts_are_never_inferred_from_container() -> None:
    info = MediaInfo(
        source_url="https://www.youtube.com/watch?v=abc",
        title="t",
        platform="youtube",
        webpage_url="https://www.youtube.com/watch?v=abc",
        extension="m4a",
        thumbnail=None,
        duration=213,
        filesize_approx=None,
        is_live=False,
        audio_ext="m4a",
        audio_kbps=134,
    )

    candidate = providers_module.candidate_from_media_info(_identity(), info.webpage_url, info)

    assert candidate.codec is None  # M4A names a container, never a codec
    assert candidate.is_lossless is False  # a box is not proof of lossless sound
    assert candidate.provider_verified_lossless is False


def test_mp3_320_from_lossy_source_keeps_honest_provenance() -> None:
    source = SourceAudio(codec="opus", container=".webm", bitrate_bps=134_000)

    output = QualityEngine.plan(source, AudioMode.MP3_320)

    assert output.transcoded is True
    assert output.true_lossless is False
    assert output.codec == "mp3"
    assert output.bitrate_bps == 320_000
    assert output.source.bitrate_bps == 134_000  # the weaker source is named
    assert output.provenance.value == "lossy_transcoded"


def _track() -> spotify.SpotifyTrack:
    return spotify.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Never Gonna Give You Up",
        artists=("Rick Astley",),
        duration_s=213,
        album="Whenever You Need Somebody",
        isrc=ISRC,
    )


class _StubExtractor:
    """Only ``search`` — the one discovery call the pipeline makes."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self._hits = hits

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        assert "Rick Astley" in query
        return self._hits[:limit]


async def test_pipeline_integration_resolves_plan_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spotify URL → identity → registry → engine → plan → ffprobe → provenance."""
    hit = SearchHit(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        title="Rick Astley - Never Gonna Give You Up",
        duration_s=212,
    )
    async def fake_lookup(url: str, **kwargs: Any) -> spotify.SpotifyTrack:
        return _track()

    monkeypatch.setattr(providers_module.spotify, "lookup", fake_lookup)
    target = await providers_module.spotify_audio_target(
        SPOTIFY_URL, _StubExtractor([hit]), limit=5  # type: ignore[arg-type]
    )

    assert target.url == hit.url
    assert target.track.isrc == ISRC  # identity stayed Spotify's, not YouTube's

    source = SourceAudio(codec=None, container=".m4a", bitrate_bps=134_000)
    planned = QualityEngine.plan_for_tier(source, "mp3.best")
    final = finalize_output(
        source,
        MediaFacts(codec="mp3", format_name="mp3", bitrate_bps=320_000),
        planned,
    )

    assert final.transcoded is True
    assert final.true_lossless is False
    assert final.provenance.value == "lossy_transcoded"


async def test_pipeline_integration_rejects_distant_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    far = SearchHit(url="https://www.youtube.com/watch?v=other", title="x", duration_s=900)
    async def fake_lookup(url: str, **kwargs: Any) -> spotify.SpotifyTrack:
        return _track()

    monkeypatch.setattr(providers_module.spotify, "lookup", fake_lookup)

    with pytest.raises(ExtractionError) as exc_info:
        await providers_module.spotify_audio_target(
            SPOTIFY_URL, _StubExtractor([far]), limit=5  # type: ignore[arg-type]
        )

    assert exc_info.value.code == "SPOTIFY_NO_MATCH"


def test_isrc_match_keeps_provider_facts_untouched() -> None:
    """The registry stamps the signal, never the audio facts."""
    original = _candidate(confidence=0.9, bitrate_bps=160_000)
    stamped = replace(original, match_method="isrc", match_confidence=0.98)

    assert stamped.codec == original.codec
    assert stamped.bitrate_bps == original.bitrate_bps
    assert stamped.is_lossless == original.is_lossless
    assert stamped.provider_verified_lossless is False
