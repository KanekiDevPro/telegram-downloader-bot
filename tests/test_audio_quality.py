"""Audio quality architecture: identity, candidates, engine, provenance, cache.

A mapped stand-in serves lossy audio; the pipeline must know that forever:
identity (who the song is) travels apart from retrieval (where bytes come
from), source facts are never overwritten by output facts, and no lossy source
may ever be labeled True Lossless — whatever container it is poured into.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
from pathlib import Path

from services import cache as cache_module
from services import providers as providers_module
from services import quality as quality_module
from services.audio_models import (
    AudioCandidate,
    AudioMode,
    OutputAudio,
    Provenance,
    SourceAudio,
    TrackIdentity,
)
from services.quality import FakeLosslessRejected, QualityEngine
from services.verify import MediaFacts

LOSSY_OPUS = SourceAudio(
    codec="opus",
    container=".webm",
    bitrate_bps=128_000,
    sample_rate=48_000,
    channels=2,
    is_lossless=False,
    provider_verified_lossless=False,
)
VERIFIED_FLAC = SourceAudio(
    codec="flac",
    container=".flac",
    bitrate_bps=2_304_000,
    sample_rate=96_000,
    bit_depth=24,
    channels=2,
    is_lossless=True,
    provider_verified_lossless=True,
)


def _identity(confidence: float = 0.99) -> TrackIdentity:
    return TrackIdentity(
        provider="spotify",
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        isrc="USSM19999999",
        artist="Rick Astley",
        title="Never Gonna Give You Up",
        album="Whenever You Need Somebody",
        duration_s=213,
        match_method="spotify-id",
        match_confidence=confidence,
    )


def _candidate(
    provider: str,
    *,
    codec: str | None,
    bitrate: int | None,
    lossless: bool,
    verified: bool,
    confidence: float,
    rate: int | None = 48_000,
    depth: int | None = None,
) -> AudioCandidate:
    return AudioCandidate(
        identity=_identity(confidence),
        provider_name=provider,
        provider_track_id=f"{provider}:track:1",
        codec=codec,
        container=".flac" if codec == "flac" else ".webm",
        bitrate_bps=bitrate,
        sample_rate=rate,
        bit_depth=depth,
        channels=2,
        is_lossless=lossless,
        provider_verified_lossless=verified,
        match_method="duration+title",
        match_confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Output planning: source and output stay separate, fakes are rejected
# ---------------------------------------------------------------------------


def test_flac_source_to_flac_is_true_lossless_native() -> None:
    out = QualityEngine.plan(VERIFIED_FLAC, AudioMode.TRUE_LOSSLESS)

    assert isinstance(out, OutputAudio)
    assert out.transcoded is False
    assert out.true_lossless is True
    assert out.provenance == Provenance.LOSSLESS_NATIVE
    assert out.codec == "flac"


def test_opus128_to_original_is_native_lossy() -> None:
    out = QualityEngine.plan(LOSSY_OPUS, AudioMode.ORIGINAL)

    assert out.transcoded is False
    assert out.true_lossless is False
    assert out.provenance == Provenance.LOSSY_NATIVE
    assert out.codec == "opus", "the source codec rides through untouched"
    assert out.source.bitrate_bps == 128_000, "source facts are kept, not overwritten"


def test_opus128_to_mp3_320_keeps_honest_provenance() -> None:
    out = QualityEngine.plan(LOSSY_OPUS, AudioMode.MP3_320)

    assert out.source.codec == "opus"
    assert out.source.bitrate_bps == 128_000
    assert out.codec == "mp3"
    assert out.bitrate_bps == 320_000
    assert out.transcoded is True
    assert out.true_lossless is False, "a bigger number is not restored information"
    assert out.provenance == Provenance.LOSSY_TRANSCODED


def test_opus128_to_flac_is_rejected_as_fake_lossless() -> None:
    try:
        QualityEngine.plan(LOSSY_OPUS, AudioMode.TRUE_LOSSLESS)
    except FakeLosslessRejected:
        return
    raise AssertionError("a lossy source must never plan a True Lossless output")


def test_unverified_flac_is_not_true_lossless() -> None:
    """A .flac container nobody verified is a claim, not a fact (never trust ext)."""
    claimed = SourceAudio(
        codec="flac",
        container=".flac",
        bitrate_bps=900_000,
        is_lossless=True,
        provider_verified_lossless=False,
    )

    try:
        QualityEngine.plan(claimed, AudioMode.TRUE_LOSSLESS)
    except FakeLosslessRejected:
        return
    raise AssertionError("unverified lossless must not pass the gate")


# ---------------------------------------------------------------------------
# Selection: confidence first, provider-independent ranking
# ---------------------------------------------------------------------------


def test_verified_lossless_provider_beats_lossy_youtube() -> None:
    youtube = _candidate("youtube", codec="opus", bitrate=160_000, lossless=False,
                         verified=False, confidence=0.99)
    hifi = _candidate("hifi", codec="flac", bitrate=2_304_000, lossless=True,
                      verified=True, confidence=0.99, rate=96_000, depth=24)

    assert QualityEngine.select([youtube, hifi]).provider_name == "hifi"


def test_low_confidence_flac_loses_to_the_exact_match() -> None:
    exact = _candidate("youtube", codec="opus", bitrate=160_000, lossless=False,
                       verified=False, confidence=0.99)
    doubtful = _candidate("hifi", codec="flac", bitrate=2_304_000, lossless=True,
                          verified=True, confidence=0.30, rate=96_000, depth=24)

    assert QualityEngine.select([exact, doubtful]).provider_name == "youtube", (
        "the wrong song in FLAC is still the wrong song"
    )


def test_ranking_is_not_bitrate_only() -> None:
    """A higher bitrate with lower confidence must not outrank the exact match."""
    exact = _candidate("youtube", codec="opus", bitrate=128_000, lossless=False,
                       verified=False, confidence=0.99)
    louder = _candidate("youtube", codec="opus", bitrate=320_000, lossless=False,
                        verified=False, confidence=0.40)

    ranked = QualityEngine.rank([louder, exact])
    assert ranked[0].match_confidence == 0.99


def test_the_engine_names_no_provider() -> None:
    """Provider-specific behavior belongs in providers, never in the ranking."""
    source = inspect.getsource(quality_module).lower()
    for name in ("youtube", "spotify", "qobuz", "tidal", "deezer", "cobalt", "yt-dlp", "ytdlp"):
        assert name not in source, f"provider branching belongs in providers, not quality: {name!r}"


def test_confidence_and_quality_travel_apart() -> None:
    candidate = _candidate("youtube", codec="opus", bitrate=160_000, lossless=False,
                           verified=False, confidence=0.99)

    assert candidate.match_confidence == 0.99
    assert candidate.bitrate_bps == 160_000
    assert candidate.identity.match_confidence == 0.99


# ---------------------------------------------------------------------------
# Identity: ISRC best-effort, never a blocker
# ---------------------------------------------------------------------------


def test_isrc_is_preserved_but_never_required() -> None:
    with_isrc = providers_module.spotify_track_identity(
        _spotify_track(isrc="USSM19999999")
    )
    without_isrc = providers_module.spotify_track_identity(_spotify_track(isrc=None))

    assert with_isrc.isrc == "USSM19999999"
    assert with_isrc.match_confidence == 1.0, "the platform's own id is exact"
    assert without_isrc.isrc is None
    assert without_isrc.track_id == "4uLU6hMCjMI75M1A2tKUQC"


def _spotify_track(isrc: str | None):  # type: ignore[no-untyped-def]
    from services import spotify

    return spotify.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Never Gonna Give You Up",
        artists=("Rick Astley",),
        duration_s=213,
        album="Whenever You Need Somebody",
        year=1987,
        isrc=isrc,
    )


# ---------------------------------------------------------------------------
# Cache: modes cannot share artifacts
# ---------------------------------------------------------------------------


def test_original_mp3_and_lossless_keys_never_collide() -> None:
    url = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"

    original = cache_module.cache_key(url, "audio", "best")
    hq = cache_module.cache_key(url, "audio", "mp3.best")
    lossless = cache_module.cache_key(url, "audio", "mp3.best", qualifier="verified-lossless")

    assert len({original, hq, lossless}) == 3


def test_legacy_cache_keys_are_untouched() -> None:
    """Existing rows keep their keys: the qualifier is additive, never a rewrite."""
    from core.utils import canonical_url, sha256_hex

    url = "https://youtu.be/abc"
    assert cache_module.cache_key(url, "audio", "mp3.best") == sha256_hex(
        f"{canonical_url(url)}|audio:mp3.best"
    )
    # Bare "best" is not an audio tier — it normalizes to the default, whose
    # key older rows already own. The qualifier must not disturb either shape.
    assert cache_module.cache_key(url, "audio", "best") == sha256_hex(
        f"{canonical_url(url)}|audio"
    )


# ---------------------------------------------------------------------------
# ffprobe finalize: the measured file is authoritative
# ---------------------------------------------------------------------------


def test_finalize_uses_measured_props_not_requested_ones() -> None:
    """Requested 320, measured 128: the output says 128, still transcoded, not lossless."""
    planned = QualityEngine.plan(LOSSY_OPUS, AudioMode.MP3_320)
    facts = MediaFacts(codec="mp3", bitrate_bps=128_000, sample_rate=44_100, channels=2)

    out = quality_module.finalize_output(LOSSY_OPUS, facts, planned)

    assert out.bitrate_bps == 128_000, "the file's own number wins"
    assert out.codec == "mp3"
    assert out.transcoded is True
    assert out.true_lossless is False
    assert out.provenance == Provenance.LOSSY_TRANSCODED


def test_finalize_without_facts_keeps_the_plan_honestly() -> None:
    """No probe (ffprobe missing) is 'unknown measurement', never invented facts."""
    planned = QualityEngine.plan(LOSSY_OPUS, AudioMode.ORIGINAL)

    out = quality_module.finalize_output(LOSSY_OPUS, None, planned)

    assert out.codec == "opus"
    assert out.transcoded is False
    assert out.true_lossless is False


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    assert exe is not None, "ffmpeg builds the artifact under test"
    return exe


def test_spotify_flac_request_is_refused_before_any_work() -> None:
    """A lossless-container ask on a track dies as LOSSLESS_UNAVAILABLE, not FLAC."""
    from services import worker as worker_module
    from services.extractor import ExtractionError

    try:
        worker_module.spotify_audio_plan(SourceAudio(), "flac")
    except ExtractionError as exc:
        assert exc.code == "LOSSLESS_UNAVAILABLE"
        return
    raise AssertionError("fake lossless must be refused, never downloaded")


def test_spotify_mp3_and_best_requests_plan_cleanly() -> None:
    from services import worker as worker_module

    hq = worker_module.spotify_audio_plan(SourceAudio(), "mp3.best")
    original = worker_module.spotify_audio_plan(SourceAudio(), "m4a")

    assert (hq.codec, hq.bitrate_bps, hq.transcoded) == ("mp3", 320_000, True)
    assert (original.transcoded, original.true_lossless) == (False, False)


def test_bare_best_is_a_transcode_request_not_a_copy() -> None:
    """Bare ``best`` is not an audio tier (it normalizes to the mp3 default) —
    which is exactly why the original rows request the native ``m4a`` tier."""
    out = QualityEngine.plan_for_tier(SourceAudio(), "best")

    assert out.transcoded is True
    assert out.true_lossless is False


def test_explicit_true_lossless_mode_needs_a_verified_source() -> None:
    from services import worker as worker_module
    from services.extractor import ExtractionError

    try:
        worker_module.spotify_audio_plan(
            LOSSY_OPUS, "mp3.best", explicit_mode="true_lossless"
        )
    except ExtractionError as exc:
        assert exc.code == "LOSSLESS_UNAVAILABLE"
        return
    raise AssertionError("explicit lossless without a verified source must refuse")


def test_tasks_carry_an_explicit_audio_mode_slot() -> None:
    from services.queue import DownloadTask

    task = DownloadTask(
        url="https://open.spotify.com/track/x", telegram_id=1, chat_id=1,
        media_format="audio", quality="mp3.best", audio_mode="true_lossless",
    )

    assert task.audio_mode == "true_lossless"
    legacy = DownloadTask(
        url="https://youtu.be/x", telegram_id=1, chat_id=1,
    )
    assert legacy.audio_mode == "", "old payloads keep working, mode unset"


async def test_finalize_of_a_real_mp3_reports_the_real_file(tmp_path: Path) -> None:
    from services.verify import probe_media

    src = tmp_path / "tone.mp3"
    subprocess.run(
        [_ffmpeg(), "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", "-b:a", "128k",
         str(src)],
        check=True, timeout=120,
    )
    facts = await probe_media(src)
    assert facts is not None and facts.codec == "mp3"

    planned = QualityEngine.plan(LOSSY_OPUS, AudioMode.MP3_320)
    out = quality_module.finalize_output(LOSSY_OPUS, facts, planned)

    assert out.codec == "mp3" and out.transcoded is True and out.true_lossless is False
    assert out.bitrate_bps is not None and 90_000 <= out.bitrate_bps <= 192_000, (
        f"measured ~128k file, got {out.bitrate_bps}"
    )
