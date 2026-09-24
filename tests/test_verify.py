"""Delivery verification: the produced file must keep the caption's promise.

Every confirmed substitution class is pinned here — the FLAC button that became
an MP3, the «320 kbps» label over a 192 kbps file — and so is the policy around
the checks: a missing ffprobe is not a failed file (fail-open, logged once), an
unreadable probe answer proves nothing, VBR inside tolerance is a kept promise,
and lossless containers are judged by codec alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services import verify, worker
from services.extractor import DownloadResult, ExtractionError
from services.queue import DownloadTask
from services.verify import MediaFacts, check_produced


def _facts(**over: Any) -> MediaFacts:
    base: dict[str, Any] = {"format_name": "mp3", "codec": "mp3", "bitrate_bps": 320_000}
    base.update(over)
    return MediaFacts(**base)


# ---------------------------------------------------------------------------
# Audio: the container and rate the label names
# ---------------------------------------------------------------------------


def test_a_correct_mp3_delivers_as_captioned() -> None:
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=318_400),
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is None
    )


def test_a_wrong_mp3_bitrate_is_a_mismatch_not_a_relabel() -> None:
    # The confirmed live bug: a «320 kbps» tap delivered a 192 kbps file.
    mismatch = check_produced(
        _facts(codec="mp3", bitrate_bps=192_000),
        media_format="audio",
        quality="mp3.best",
        suffix=".mp3",
    )
    assert mismatch is not None
    assert "320" in mismatch and "192" in mismatch


def test_a_real_flac_passes_on_codec_alone() -> None:
    # Lossless has no bitrate knob — even a wild average proves nothing wrong.
    assert (
        check_produced(
            _facts(format_name="flac", codec="flac", bitrate_bps=987_654),
            media_format="audio",
            quality="flac",
            suffix=".flac",
        )
        is None
    )


def test_a_flac_label_over_an_mp3_file_is_refused() -> None:
    # The confirmed live bug: FLAC selected, MP3 delivered.
    mismatch = check_produced(
        _facts(format_name="mp3", codec="mp3", bitrate_bps=192_000),
        media_format="audio",
        quality="flac",
        suffix=".flac",
    )
    assert mismatch is not None
    assert "codec" in mismatch


def test_a_correct_opus_delivers_as_captioned() -> None:
    assert (
        check_produced(
            _facts(format_name="ogg", codec="opus", bitrate_bps=126_000),
            media_format="audio",
            quality="opus.high",
            suffix=".opus",
        )
        is None
    )


def test_a_copied_stream_is_never_judged_by_a_bitrate() -> None:
    # «M4A · Original» claims no rate — the site's stream is the site's stream.
    assert (
        check_produced(
            _facts(format_name="mov,mp4", codec="aac", bitrate_bps=51_000),
            media_format="audio",
            quality="m4a",
            suffix=".m4a",
        )
        is None
    )


@pytest.mark.parametrize(
    "measured_bps",
    (
        232_000,  # 0.725× — VBR dips inside tolerance
        224_000,  # exactly 0.7× — the edge itself
        470_000,  # 1.47× — overhead/padding upward
    ),
)
def test_vbr_inside_tolerance_is_a_kept_promise(measured_bps: int) -> None:
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=measured_bps),
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is None
    )


def test_a_file_far_below_its_promise_fails_closed() -> None:
    mismatch = check_produced(
        _facts(codec="mp3", bitrate_bps=220_000),
        media_format="audio",
        quality="mp3.best",
        suffix=".mp3",
    )
    assert mismatch is not None and "bitrate" in mismatch


def test_no_bitrate_is_claimed_when_the_container_is_not_the_tier_s() -> None:
    # A .m4a file answering an mp3 tap is named M4A — no rate to check.
    assert (
        check_produced(
            _facts(format_name="mov,mp4", codec="aac", bitrate_bps=64_000),
            media_format="audio",
            quality="mp3.best",
            suffix=".m4a",
        )
        is None
    )


def test_an_unreported_codec_proves_nothing() -> None:
    assert (
        check_produced(
            _facts(codec=""),
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is None
    )


# ---------------------------------------------------------------------------
# Video: the resolution the label names
# ---------------------------------------------------------------------------


def test_a_correct_video_resolution_delivers_as_captioned() -> None:
    assert (
        check_produced(
            _facts(format_name="mp4", codec="h264", width=1920, height=1080),
            media_format="video",
            quality="1080",
            suffix=".mp4",
            produced_p=1080,
        )
        is None
    )


def test_coded_padding_is_not_a_resolution_lie() -> None:
    assert (
        check_produced(
            _facts(codec="h264", width=1920, height=1088),
            media_format="video",
            quality="1080",
            suffix=".mp4",
            produced_p=1080,
        )
        is None
    )


def test_a_mismatched_video_resolution_is_refused() -> None:
    mismatch = check_produced(
        _facts(codec="h264", width=1280, height=720),
        media_format="video",
        quality="1080",
        suffix=".mp4",
        produced_p=1080,
    )
    assert mismatch is not None
    assert "1080" in mismatch and "720" in mismatch


def test_an_unclaimed_video_resolution_is_not_checked() -> None:
    assert (
        check_produced(
            _facts(codec="h264", height=360),
            media_format="video",
            quality="best",
            suffix=".mp4",
            produced_p=None,
        )
        is None
    )


# ---------------------------------------------------------------------------
# The policy: fail open when nothing can be learned
# ---------------------------------------------------------------------------


async def test_ffprobe_being_absent_is_unavailable_not_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(verify.shutil, "which", lambda _name: None)
    produced = tmp_path / "song.flac"
    produced.write_bytes(b"\x00")
    assert await verify.verify_produced(produced, media_format="audio", quality="flac") is None


def test_a_malformed_probe_answer_is_unreadable_not_a_mismatch() -> None:
    assert verify._parse_probe(b"not json at all") is None
    assert verify._parse_probe(json.dumps({"format": {}, "streams": "oops"}).encode()) is None
    assert verify._parse_probe(json.dumps({"format": {}}).encode()) is None


def test_a_file_with_no_media_stream_is_a_mismatch_not_an_unreadable_answer() -> None:
    """A *readable* probe reporting zero media streams measured the truth: a
    container with nothing in it is not the file anyone was promised.

    (Contract change, deliberate: this exact shape — ``{"streams": []}`` — used
    to be classed "unreadable" and failed open. Stream existence is part of the
    delivery contract now.)
    """
    facts = verify._parse_probe(json.dumps({"streams": []}).encode())
    assert facts is not None and facts.media_streams == 0
    mismatch = check_produced(facts, media_format="audio", quality="flac", suffix=".flac")
    assert mismatch is not None and "stream" in mismatch


async def test_a_missing_or_empty_output_is_refused_before_any_probe(tmp_path: Path) -> None:
    missing = tmp_path / "gone.mp3"
    mismatch = await verify.verify_produced(missing, media_format="audio", quality="mp3.best")
    assert mismatch is not None and "missing or empty" in mismatch

    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    mismatch = await verify.verify_produced(empty, media_format="audio", quality="mp3.best")
    assert mismatch is not None and "missing or empty" in mismatch


# ---------------------------------------------------------------------------
# The user's selection is a claim too — and an upscale is only an observation
# ---------------------------------------------------------------------------


def test_a_delivered_rung_below_the_selected_one_fails_clearly() -> None:
    """A 720p tap that lands on 480p (the format vanished before the download)
    is refused — never silently re-captioned to whatever arrived."""
    mismatch = check_produced(
        _facts(format_name="mp4", codec="h264", width=854, height=480),
        media_format="video",
        quality="720",
        suffix=".mp4",
        produced_p=480,
        selected_p=720,
    )
    assert mismatch is not None and "selected 720p" in mismatch


def test_a_delivered_rung_matching_the_selection_passes() -> None:
    assert (
        check_produced(
            _facts(format_name="mp4", codec="h264", width=1280, height=720),
            media_format="video",
            quality="720",
            suffix=".mp4",
            produced_p=720,
            selected_p=720,
        )
        is None
    )


def test_an_unselected_rung_makes_no_selection_claim() -> None:
    '''A "best" request names no rung: only the caption's own claim is checked.'''
    assert (
        check_produced(
            _facts(format_name="mp4", codec="h264", width=854, height=480),
            media_format="video",
            quality="best",
            suffix=".mp4",
            produced_p=480,
        )
        is None
    )


async def test_a_source_upscale_is_observed_and_never_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 320 kbps target over a ~130 kbps source is delivered as requested —
    the observation is a log line, not a verdict and not a user-facing state."""

    async def fake_probe(path: Path) -> MediaFacts:
        return _facts(codec="mp3", bitrate_bps=318_400, media_streams=1)

    monkeypatch.setattr(verify, "probe_media", fake_probe)
    produced = tmp_path / "song.mp3"
    produced.write_bytes(b"\x00" * 32)

    with caplog.at_level("INFO", logger="services.verify"):
        mismatch = await verify.verify_produced(
            produced, media_format="audio", quality="mp3.best", source_kbps=130
        )
    assert mismatch is None, "the requested encoding target was produced"
    assert "upscale observed" in caplog.text

    # …and an unknown source rate infers nothing in either direction.
    caplog.clear()
    with caplog.at_level("INFO", logger="services.verify"):
        mismatch = await verify.verify_produced(
            produced, media_format="audio", quality="mp3.best", source_kbps=None
        )
    assert mismatch is None and "upscale" not in caplog.text


def test_unknown_probe_fields_never_guess() -> None:
    facts = verify._parse_probe(
        json.dumps(
            {
                "format": {"format_name": "mp3", "bit_rate": "not-a-number"},
                "streams": [{"codec_type": "audio", "codec_name": "mp3"}],
            }
        ).encode()
    )
    assert facts is not None
    assert facts.bitrate_bps is None and facts.duration_s is None
    # …and a file with no measurable rate contradicts nothing.
    assert check_produced(facts, media_format="audio", quality="mp3.best", suffix=".mp3") is None


# ---------------------------------------------------------------------------
# The wiring: the worker refuses before it uploads
# ---------------------------------------------------------------------------


def _result(tmp_path: Path, quality: str = "mp3.best") -> DownloadResult:
    job = tmp_path / "job-x"
    job.mkdir()
    produced = job / "song.mp3"
    produced.write_bytes(b"\x00")
    info = type(
        "Info",
        (),
        {
            "label_p": None,
            "height": None,
            "title": "Song",
            "platform": "test",
            "duration": 0,
            "audio_kbps": None,
        },
    )()
    return DownloadResult(
        file_path=produced,
        info=info,
        media_format="audio",
        quality=quality,
    )


def _task() -> DownloadTask:
    return DownloadTask(
        chat_id=5,
        telegram_id=5,
        url="https://example.com/a",
        media_format="audio",
        lang="en",
        title="Song",
        chat_title="",
    )


async def test_the_worker_delivers_no_lie_even_mid_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A measured contradiction stops the job before a caption can be sent."""

    async def fake_verify(path: Path, **_claim: Any) -> str:
        return "codec: measured mp3, claimed flac"

    monkeypatch.setattr(worker.verify, "verify_produced", fake_verify)
    with pytest.raises(ExtractionError) as caught:
        await worker._finish_upload(
            _task(), None, object(), None, _result(tmp_path, quality="flac")  # type: ignore[arg-type]
        )
    assert caught.value.code == "CONVERSION_MISMATCH"


async def test_the_worker_verifies_against_the_users_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """What reaches verification is the *selection* — the rung the tap promised
    — plus the source rate for the upscale observation. A vanished format is a
    clear failure here, never a silent re-caption."""
    seen: dict[str, Any] = {}

    async def fake_verify(path: Path, **claim: Any) -> str:
        seen.update(claim)
        return "resolution: selected 720p, measured height 480"

    monkeypatch.setattr(worker.verify, "verify_produced", fake_verify)
    job = tmp_path / "job-y"
    job.mkdir()
    produced = job / "clip.mp4"
    produced.write_bytes(b"\x00")
    info = type(
        "Info",
        (),
        {
            "label_p": 480,
            "height": 480,
            "title": "Clip",
            "platform": "test",
            "duration": 0,
            "audio_kbps": 130,
        },
    )()
    result = DownloadResult(
        file_path=produced, info=info, media_format="video", quality="720"
    )
    task = DownloadTask(
        chat_id=5,
        telegram_id=5,
        url="https://example.com/v",
        media_format="video",
        quality="720",
        lang="en",
        title="Clip",
        chat_title="",
    )

    with pytest.raises(ExtractionError) as caught:
        await worker._finish_upload(task, None, object(), None, result)  # type: ignore[arg-type]

    assert caught.value.code == "CONVERSION_MISMATCH"
    assert seen["selected_p"] == "720", "the user's selection is the claim"
    assert seen["produced_p"] == 480, "and so is what the caption would say"
    assert seen["source_kbps"] == 130, "the source rate rides along (observation only)"


# ---------------------------------------------------------------------------
# Nominal tiers: every lossy rung keeps its promise inside the window
# (required cases 1-4 — 320 is pinned above, in test_a_correct_mp3_delivers…)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("quality", "measured_bps"),
    (
        ("mp3.high", 255_200),   # 256 kbps tier, 0.997×
        ("mp3", 191_000),        # 192 kbps tier
        ("mp3.small", 127_500),  # 128 kbps tier
    ),
)
def test_each_nominal_tier_delivers_as_captioned(quality: str, measured_bps: int) -> None:
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=measured_bps),
            media_format="audio",
            quality=quality,
            suffix=".mp3",
        )
        is None
    )


# Case 7: small normal measurement variance is a kept promise (±1% here; the
# wider VBR window is pinned in test_vbr_inside_tolerance_is_a_kept_promise).
def test_small_measurement_variance_is_a_kept_promise() -> None:
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=322_400),  # +0.75%
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is None
    )


# Case 8: a VBR target may legitimately average under its nominal rate.
def test_a_vbr_target_averaging_low_inside_the_window_is_not_a_mismatch() -> None:
    assert (
        check_produced(
            _facts(format_name="ogg", codec="opus", bitrate_bps=144_000),  # 0.75× of 192
            media_format="audio",
            quality="opus.best",
            suffix=".opus",
        )
        is None
    )


# The window itself: inclusive at both documented edges, no scattered math.
def test_the_bitrate_window_is_inclusive_at_both_edges() -> None:
    assert verify.bitrate_in_window(224.0, 320) is True   # exactly 0.7×
    assert verify.bitrate_in_window(480.0, 320) is True   # exactly 1.5×
    assert verify.bitrate_in_window(223.9, 320) is False  # materially below
    assert verify.bitrate_in_window(480.1, 320) is False  # above the label


# ---------------------------------------------------------------------------
# Source-quality ceiling: a local observation, never a delivery verdict
# (required cases 5, 6, 10)
# ---------------------------------------------------------------------------


def test_a_source_upscale_is_an_observation_not_a_conversion_mismatch() -> None:
    # ~130 kbps AAC source, 320 kbps MP3 requested and delivered: the encoding
    # target was produced, so delivery passes …
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=319_000),
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is None
    )
    # … while the comparison helper — and only it — sees the upscale.
    assert verify.is_source_upscale(320, 130) is True


def test_a_request_within_the_source_rate_is_not_an_upscale() -> None:
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=128_500),
            media_format="audio",
            quality="mp3.small",
            suffix=".mp3",
        )
        is None
    )
    assert verify.is_source_upscale(128, 130) is False


def test_an_unknown_source_rate_infers_nothing_in_either_direction() -> None:
    # Unknown source ⇒ undetermined — never "upscale", never "no upscale".
    assert verify.is_source_upscale(320, None) is None
    assert verify.is_source_upscale(320, 0) is None
    # And the requested tier is still validated without any source knowledge.
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=192_000),
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Lossless and the measured-but-uncaptioned fields (required cases 13, 14)
# ---------------------------------------------------------------------------


def test_a_real_wav_passes_on_its_pcm_family_alone() -> None:
    assert (
        check_produced(
            _facts(format_name="wav", codec="pcm_s16le", bitrate_bps=1_411_200),
            media_format="audio",
            quality="wav",
            suffix=".wav",
        )
        is None
    )


def test_a_non_pcm_codec_in_a_wav_container_is_still_a_mismatch() -> None:
    mismatch = check_produced(
        _facts(format_name="wav", codec="mp3", bitrate_bps=192_000),
        media_format="audio",
        quality="wav",
        suffix=".wav",
    )
    assert mismatch is not None and "codec" in mismatch


def test_sample_rate_and_channels_are_measured_but_never_contradict() -> None:
    # Existing policy, pinned: the caption claims format and rate only, so a
    # measured-but-uncaptioned sample rate or channel count proves nothing —
    # "only what the file says is checked" against what the caption says.
    assert (
        check_produced(
            _facts(codec="mp3", bitrate_bps=319_000, sample_rate=22_050, channels=1),
            media_format="audio",
            quality="mp3.best",
            suffix=".mp3",
        )
        is None
    )


# Case 15: the video path is untouched by the audio work.
def test_video_verification_is_unchanged() -> None:
    assert (
        check_produced(
            _facts(codec="h264", width=3840, height=2160),
            media_format="video",
            quality="2160",
            suffix=".mp4",
            produced_p=2160,
        )
        is None
    )
    mismatch = check_produced(
        _facts(codec="h264", width=3840, height=2160),
        media_format="video",
        quality="1080",
        suffix=".mp4",
        produced_p=1080,
    )
    assert mismatch is not None and "1080" in mismatch


# ---------------------------------------------------------------------------
# The documented height tolerance (±5% of the claim, at least 16 px)
# ---------------------------------------------------------------------------


def test_the_height_tolerance_is_documented_and_absorbs_only_near_misses() -> None:
    """Coded padding and normal variance live inside the tolerance; another
    rung never does. The numbers are the documented contract (verify.py module
    docstring, docs/RELEASE_RUNBOOK.md §7) — pinned so they cannot drift."""
    assert verify.HEIGHT_TOLERANCE_PX == 16
    assert verify.HEIGHT_TOLERANCE_RATIO == 0.05

    # 720p allows 36 px (5%): 690 is inside, 683 is a different rung.
    assert (
        check_produced(
            _facts(codec="h264", width=1226, height=690),
            media_format="video",
            quality="720",
            suffix=".mp4",
            produced_p=720,
            selected_p=720,
        )
        is None
    )
    mismatch = check_produced(
        _facts(codec="h264", width=1214, height=683),
        media_format="video",
        quality="720",
        suffix=".mp4",
        produced_p=720,
        selected_p=720,
    )
    assert mismatch is not None and "683" in mismatch


def test_a_720p_selection_is_never_delivered_or_captioned_as_480p() -> None:
    """The exact release case: 720p tapped, 480p produced. The check fails the
    delivery (the worker raises before any upload — see the wiring cases above)
    and the caption could only ever have said 480p
    (tests/test_delivery_honesty.py)."""
    mismatch = check_produced(
        _facts(codec="h264", width=854, height=480),
        media_format="video",
        quality="720",
        suffix=".mp4",
        produced_p=480,
        selected_p=720,
    )
    assert mismatch is not None
    assert "selected 720p" in mismatch and "480" in mismatch
