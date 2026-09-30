"""Production audio pipeline: registry wiring, cache isolation, remux, codec hygiene.

These tests pin the *production* path, not the models in isolation: the Spotify
audio flow must go through ProviderRegistry + QualityEngine, cache reads must
carry the same quality identity as writes, and lossless classification must
stay provenance-based. Fakes stand at the network boundaries only (Spotify page,
yt-dlp search); everything between them is the real production code.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from services import cache as cache_module
from services import providers as providers_module
from services import quality as quality_module
from services.audio_models import (
    AudioCandidate,
    AudioMode,
    Provenance,
    SourceAudio,
    TrackIdentity,
)
from services.extractor import ExtractionError, SearchHit
from services.providers import ProviderRegistry
from services.quality import LOSSLESS_QUALIFIER, QualityEngine

SPOTIFY_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def _track(duration_s: int | None = 213):  # type: ignore[no-untyped-def]
    from services import spotify

    return spotify.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Never Gonna Give You Up",
        artists=("Rick Astley",),
        duration_s=duration_s,
        album="Whenever You Need Somebody",
        year=1987,
    )


def _hit(url: str = "https://youtu.be/abc", duration_s: int | None = 213) -> SearchHit:
    return SearchHit(url=url, title="Rick Astley - Never Gonna Give You Up", duration_s=duration_s)


class _FakeExtractor:
    """Stands in for yt-dlp search only — never for ranking or planning."""

    def __init__(self, hits: list[SearchHit]) -> None:
        self.hits = hits
        self.queries: list[str] = []

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits[:limit]


def _identity() -> TrackIdentity:
    return providers_module.spotify_track_identity(_track())


# ---------------------------------------------------------------------------
# Registry failure isolation
# ---------------------------------------------------------------------------


class _FailingProvider:
    name = "failing"

    async def search_by_isrc(self, isrc: str) -> list[AudioCandidate]:
        raise RuntimeError("boom")

    async def search_by_metadata(self, identity: TrackIdentity) -> list[AudioCandidate]:
        raise RuntimeError("boom")

    async def resolve(self, candidate: AudioCandidate) -> str:
        raise RuntimeError("boom")


async def test_registry_skips_a_failing_provider() -> None:
    identity = _identity()
    good_hit = _hit()
    registry = ProviderRegistry()
    registry.register(cast(Any, _FailingProvider()))
    registry.register(
        providers_module.YouTubeProvider(cast(Any, _FakeExtractor([good_hit])))
    )

    found = await registry.candidates(identity)

    assert [c.provider_track_id for c in found] == [good_hit.url]


# ---------------------------------------------------------------------------
# Production Spotify target: registry + engine wired in
# ---------------------------------------------------------------------------


async def test_spotify_audio_path_uses_registry_and_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production resolver must discover via the registry and pick via the
    engine — a bypass of either must fail this test."""
    from services import spotify

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return _track()

    monkeypatch.setattr(spotify, "lookup", fake_lookup)

    extractor = _FakeExtractor([_hit()])
    calls: list[str] = []
    real_candidates = ProviderRegistry.candidates
    real_select = QualityEngine.select

    async def spied_candidates(self: ProviderRegistry, identity: TrackIdentity) -> list[AudioCandidate]:
        calls.append("registry.candidates")
        return await real_candidates(self, identity)

    def spied_select(candidates: list[AudioCandidate]) -> AudioCandidate:
        calls.append("engine.select")
        return real_select(candidates)

    monkeypatch.setattr(ProviderRegistry, "candidates", spied_candidates)
    monkeypatch.setattr(QualityEngine, "select", spied_select)

    target = await providers_module.spotify_audio_target(SPOTIFY_URL, cast(Any, extractor))

    assert target.url == "https://youtu.be/abc"
    assert target.track.track_id == "4uLU6hMCjMI75M1A2tKUQC"
    assert calls == ["registry.candidates", "engine.select"]
    assert extractor.queries, "discovery must actually search the provider"


async def test_spotify_audio_target_rejects_a_distant_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import spotify

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return _track()

    monkeypatch.setattr(spotify, "lookup", fake_lookup)
    extractor = _FakeExtractor([_hit(url="https://youtu.be/long", duration_s=213 + 500)])

    try:
        await providers_module.spotify_audio_target(SPOTIFY_URL, cast(Any, extractor))
    except ExtractionError as exc:
        assert exc.code == "SPOTIFY_NO_MATCH"
        return
    raise AssertionError("a 500s-distant hit must not become the track")


async def test_spotify_audio_candidates_are_never_verified_lossless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring the architecture must not invent a lossless YouTube source."""
    from services import spotify

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return _track()

    monkeypatch.setattr(spotify, "lookup", fake_lookup)
    extractor = _FakeExtractor([_hit()])

    target = await providers_module.spotify_audio_target(SPOTIFY_URL, cast(Any, extractor))
    identity = providers_module.spotify_track_identity(target.track)
    registry = providers_module.build_default_registry(cast(Any, extractor))

    for candidate in await registry.candidates(identity):
        assert candidate.provider_verified_lossless is False
        assert candidate.is_lossless is False


# ---------------------------------------------------------------------------
# Cache read/write isolation: one canonical derivation
# ---------------------------------------------------------------------------


def test_qualifier_derivation_is_canonical() -> None:
    assert cache_module.qualifier_for("audio", "m4a") == ""
    assert cache_module.qualifier_for("audio", "mp3.best") == ""
    assert cache_module.qualifier_for("audio", "mp3.best", audio_mode="true_lossless") == (
        LOSSLESS_QUALIFIER
    )
    assert cache_module.qualifier_for("audio", "flac", audio_mode="true_lossless") == (
        LOSSLESS_QUALIFIER
    )
    assert quality_module.LOSSLESS_QUALIFIER == LOSSLESS_QUALIFIER


def test_lossless_and_lossy_keys_never_collide() -> None:
    lossy = cache_module.cache_key(SPOTIFY_URL, "audio", "mp3.best")
    lossless = cache_module.cache_key(
        SPOTIFY_URL, "audio", "mp3.best", qualifier=LOSSLESS_QUALIFIER
    )
    original = cache_module.cache_key(SPOTIFY_URL, "audio", "m4a")
    derived = cache_module.cache_key(
        SPOTIFY_URL,
        "audio",
        "mp3.best",
        qualifier=cache_module.qualifier_for("audio", "mp3.best", audio_mode="true_lossless"),
    )

    assert len({lossy, lossless, original}) == 3
    assert derived == lossless, "reads must derive the same key writes stored"


@pytest.mark.parametrize(
    ("quality", "audio_mode", "other_quality", "other_mode"),
    [
        ("m4a", "", "mp3.best", ""),
        ("mp3.best", "", "m4a", ""),
        ("mp3.best", "true_lossless", "mp3.best", ""),
        ("mp3.best", "", "mp3.best", "true_lossless"),
        ("m4a", "true_lossless", "m4a", ""),
    ],
)
def test_cache_matrix_modes_never_share_keys(
    quality: object, audio_mode: str, other_quality: object, other_mode: str
) -> None:
    mine = cache_module.cache_key(
        SPOTIFY_URL, "audio", quality,
        qualifier=cache_module.qualifier_for("audio", quality, audio_mode=audio_mode),
    )
    theirs = cache_module.cache_key(
        SPOTIFY_URL, "audio", other_quality,
        qualifier=cache_module.qualifier_for("audio", other_quality, audio_mode=other_mode),
    )
    assert mine != theirs


def test_legacy_keys_unchanged_by_qualifier_helper() -> None:
    from core.utils import canonical_url, sha256_hex

    assert cache_module.cache_key(SPOTIFY_URL, "audio", "m4a") == sha256_hex(
        f"{canonical_url(SPOTIFY_URL)}|audio:m4a"
    )
    assert cache_module.cache_key(
        SPOTIFY_URL, "audio", "m4a", qualifier=cache_module.qualifier_for("audio", "m4a")
    ) == sha256_hex(f"{canonical_url(SPOTIFY_URL)}|audio:m4a")


# ---------------------------------------------------------------------------
# REMUX: a genuine container-only lossless operation
# ---------------------------------------------------------------------------


def _verified_alac_m4a() -> SourceAudio:
    return SourceAudio(
        codec="alac",
        container=".m4a",
        bitrate_bps=900_000,
        sample_rate=44_100,
        bit_depth=16,
        channels=2,
        is_lossless=True,
        provider_verified_lossless=True,
    )


def test_verified_alac_remuxed_to_flac_is_lossless_remux() -> None:
    from services.verify import MediaFacts

    planned = QualityEngine.plan(_verified_alac_m4a(), AudioMode.TRUE_LOSSLESS)
    facts = MediaFacts(codec="flac", bitrate_bps=900_000, sample_rate=44_100, channels=2,
                       format_name="flac")

    out = quality_module.finalize_output(_verified_alac_m4a(), facts, planned)

    assert out.true_lossless is True
    assert out.transcoded is False
    assert out.provenance == Provenance.LOSSLESS_REMUX


def test_verified_flac_kept_in_flac_is_native_not_remux() -> None:
    from services.verify import MediaFacts

    source = SourceAudio(
        codec="flac", container=".flac", is_lossless=True, provider_verified_lossless=True,
    )
    planned = QualityEngine.plan(source, AudioMode.TRUE_LOSSLESS)
    facts = MediaFacts(codec="flac", format_name="flac")

    out = quality_module.finalize_output(source, facts, planned)

    assert out.true_lossless is True
    assert out.provenance == Provenance.LOSSLESS_NATIVE


def test_lossy_to_flac_is_never_remux_or_lossless() -> None:
    from services.verify import MediaFacts

    source = SourceAudio(codec="opus", container=".webm", is_lossless=False,
                         provider_verified_lossless=False)
    try:
        QualityEngine.plan(source, AudioMode.TRUE_LOSSLESS)
    except quality_module.FakeLosslessRejected:
        pass
    else:
        raise AssertionError("lossy must not plan lossless")

    sneaky = quality_module.finalize_output(
        source,
        MediaFacts(codec="flac", format_name="flac"),
        QualityEngine.plan(source, AudioMode.ORIGINAL),
    )
    assert sneaky.true_lossless is False
    assert sneaky.provenance != Provenance.LOSSLESS_REMUX
    assert sneaky.provenance != Provenance.LOSSLESS_NATIVE


# ---------------------------------------------------------------------------
# Codec/container hygiene: m4a is not a codec
# ---------------------------------------------------------------------------


def test_m4a_is_not_ranked_as_a_codec() -> None:
    assert "m4a" not in quality_module.CODEC_RANK
    assert "M4A" not in quality_module.CODEC_RANK
    assert "m4a" not in quality_module.LOSSLESS_CODECS


# ---------------------------------------------------------------------------
# Fake verified-lossless provider: full loop, cache-qualified (test-only)
# ---------------------------------------------------------------------------


class _FakeLosslessProvider:
    """A stand-in Hi-Fi source. Test-only: never registered in production."""

    name = "fake-hifi"

    def __init__(self, candidate: AudioCandidate) -> None:
        self._candidate = candidate

    async def search_by_isrc(self, isrc: str) -> list[AudioCandidate]:
        return [self._candidate] if self._candidate.identity.isrc == isrc else []

    async def search_by_metadata(self, identity: TrackIdentity) -> list[AudioCandidate]:
        return [self._candidate]

    async def resolve(self, candidate: AudioCandidate) -> str:
        return candidate.provider_track_id


def _verified_flac_candidate(identity: TrackIdentity) -> AudioCandidate:
    return AudioCandidate(
        identity=identity,
        provider_name="fake-hifi",
        provider_track_id="fake-hifi:track:1",
        codec="flac",
        container=".flac",
        bitrate_bps=2_304_000,
        sample_rate=96_000,
        bit_depth=24,
        channels=2,
        is_lossless=True,
        provider_verified_lossless=True,
        match_method="isrc",
        match_confidence=0.99,
    )


async def test_verified_lossless_loops_to_qualified_cache() -> None:
    identity = _identity()
    registry = ProviderRegistry()
    registry.register(cast(Any, _FakeLosslessProvider(_verified_flac_candidate(identity))))

    selected = QualityEngine.select(await registry.candidates(identity))
    assert selected.provider_name == "fake-hifi"

    source = SourceAudio(
        codec=selected.codec,
        container=selected.container,
        bitrate_bps=selected.bitrate_bps,
        sample_rate=selected.sample_rate,
        bit_depth=selected.bit_depth,
        channels=selected.channels,
        is_lossless=selected.is_lossless,
        provider_verified_lossless=selected.provider_verified_lossless,
    )
    out = QualityEngine.plan(source, AudioMode.TRUE_LOSSLESS)
    assert out.true_lossless is True

    qualifier = cache_module.qualifier_for("audio", "flac", audio_mode="true_lossless")
    qualified = cache_module.cache_key(SPOTIFY_URL, "audio", "flac", qualifier=qualifier)
    bare = cache_module.cache_key(SPOTIFY_URL, "audio", "flac")
    original = cache_module.cache_key(SPOTIFY_URL, "audio", "m4a")
    assert len({qualified, bare, original}) == 3


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    assert exe is not None, "ffmpeg builds the artifact under test"
    return exe


async def test_true_lossless_of_a_real_flac_reports_the_real_file(tmp_path: Path) -> None:
    """A genuinely lossless file (silent PCM poured into FLAC) probes lossless,
    and the verified-source plan finalizes as true lossless."""
    import wave

    from services.verify import probe_media

    wav = tmp_path / "silence.wav"
    with wave.open(str(wav), "w") as fh:
        fh.setnchannels(2)
        fh.setsampwidth(2)
        fh.setframerate(44_100)
        fh.writeframes(b"\x00" * 44_100 * 2 * 2)
    flac = tmp_path / "silence.flac"
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-i", str(wav), str(flac)],
        check=True, timeout=120,
    )
    facts = await probe_media(flac)
    assert facts is not None and facts.codec == "flac"

    source = SourceAudio(
        codec="flac", container=".flac", is_lossless=True, provider_verified_lossless=True,
    )
    out = quality_module.finalize_output(
        source, facts, QualityEngine.plan(source, AudioMode.TRUE_LOSSLESS)
    )
    assert out.true_lossless is True
    assert out.provenance in (Provenance.LOSSLESS_NATIVE, Provenance.LOSSLESS_REMUX)


async def test_production_wiring_marks_spotify_candidates_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End of the wiring contract: whatever the production registry returns for
    a Spotify identity today must not claim verified lossless."""
    from services import spotify

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return _track()

    monkeypatch.setattr(spotify, "lookup", fake_lookup)
    extractor = _FakeExtractor([_hit(), _hit(url="https://youtu.be/def", duration_s=215)])
    registry = providers_module.build_default_registry(cast(Any, extractor))

    found = await registry.candidates(_identity())

    assert found, "the production registry must produce a candidate"
    assert all(c.provider_verified_lossless is False for c in found)
    assert all(c.is_lossless is False for c in found)


def test_fallback_sibling_replay_must_not_cross_audio_tiers() -> None:
    """The auto-best sibling net may serve video family rows, but an audio ask
    must only ever replay its own exact request — never a sibling tier."""
    from handlers import user as user_module

    current = cache_module.request_key("audio", "mp3.best")
    sibling_lossless = f"{cache_module.request_key('audio', 'mp3.best')}:{LOSSLESS_QUALIFIER}"
    sibling_original = cache_module.request_key("audio", "m4a")

    assert user_module._audio_sibling_may_replay(current, sibling_lossless) is False
    assert user_module._audio_sibling_may_replay(current, sibling_original) is False
    assert user_module._audio_sibling_may_replay(current, current) is True


async def test_worker_cache_read_derives_lossless_qualifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker + handler reads must pass the qualifier, or a True Lossless ask
    could be answered from a lossy row."""
    from handlers import user as user_module
    from services import worker as worker_module

    seen: dict[str, Any] = {}

    async def fake_get_cached(pool: Any, url: str, media_format: str = "video",
                              quality: object = "", *, qualifier: str = "") -> None:
        seen["qualifier"] = qualifier
        return None

    monkeypatch.setattr(worker_module.cache_service, "get_cached", fake_get_cached)
    monkeypatch.setattr(user_module.cache_service, "get_cached", fake_get_cached)

    assert worker_module._audio_cache_qualifier("mp3.best", "true_lossless") == LOSSLESS_QUALIFIER
    assert worker_module._audio_cache_qualifier("mp3.best", "") == ""
    assert user_module._audio_cache_qualifier("mp3.best", "true_lossless") == LOSSLESS_QUALIFIER
    assert user_module._audio_cache_qualifier("mp3.best", "") == ""
