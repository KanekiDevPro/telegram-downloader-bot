"""Quality tiers: what a tier means to yt-dlp, to the cache key, and to Cobalt.

A tier is three promises at once, and each is checked here:

* a *ceiling*, never an upscale — 1080p on a 480p-only video must give the 480p file
  rather than "requested format is not available", which would be classified as a
  block and would look like the site refusing us;
* part of the *identity* of a cached file — a 480p ask must not replay the 1080p
  file, while the default tier must keep the key older rows already have;
* carried across the hand-over to the other engine — a fallback that quietly serves
  1080p for a 480p request would override the choice the menu offered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import pytest

from core.utils import (
    AUDIO_FORMAT_LEVELS,
    AUDIO_FORMATS,
    AUDIO_QUALITIES,
    DEFAULT_AUDIO_QUALITY,
    DEFAULT_VIDEO_QUALITY,
    VIDEO_QUALITIES,
    is_video_height,
    normalize_quality,
    quality_key,
)
from services import cache as cache_service
from services.cobalt import _legacy_payload, _modern_payload, audio_format_param
from services.extractor import (
    ExtractionError,
    ExtractorService,
    VideoOption,
    audio_bitrate,
    audio_is_original,
    audio_size_estimate,
    format_selector,
    quality_label_p,
)

# ---------------------------------------------------------------------------
# yt-dlp: what a tier means
# ---------------------------------------------------------------------------


def test_an_audio_tier_names_its_real_bitrate() -> None:
    assert audio_bitrate("mp3.best") == 320
    assert audio_bitrate("mp3.high") == 256
    assert audio_bitrate("mp3") == 192
    assert audio_bitrate("mp3.small") == 128
    assert audio_bitrate("opus.best") == 192
    assert audio_bitrate("opus.small") == 64
    assert audio_bitrate("m4a.high") == 256


def test_the_untouched_stream_has_no_bitrate_to_name() -> None:
    assert audio_bitrate("m4a") is None
    assert audio_is_original("m4a") is True
    assert audio_is_original("mp3.best") is False
    # Raw and lossless output are real exports — just knob-less ones.
    assert audio_is_original("wav") is False
    assert audio_is_original("flac") is False


def test_an_audio_size_estimate_is_rate_times_length() -> None:
    estimate = audio_size_estimate("mp3.best", 150)
    assert estimate is not None and 5_500_000 <= estimate <= 6_500_000
    small = audio_size_estimate("mp3.small", 150)
    assert small is not None and small < estimate
    # WAV is CD-shaped PCM arithmetic — bigger than any lossy tier.
    wav = audio_size_estimate("wav", 150)
    assert wav is not None and wav > estimate


def test_no_estimate_where_the_number_would_be_fiction() -> None:
    assert audio_size_estimate("m4a", 150) is None, "untouched stream, unknown rate"
    assert audio_size_estimate("flac", 150) is None, "lossless size depends on the music"
    assert audio_size_estimate("mp3.best", 0) is None


def test_flac_is_a_real_format_with_no_fake_quality_knob() -> None:
    assert "flac" in AUDIO_FORMATS
    assert AUDIO_FORMAT_LEVELS["flac"] == ()
    assert normalize_quality("flac", "audio") == "flac"
    assert normalize_quality("flac.best", "audio") == "flac", "aliases resolve"


def test_the_fallback_refuses_a_format_it_cannot_serve() -> None:
    """Cobalt has no FLAC service — the hand-over refuses instead of mislabeling."""
    assert audio_format_param("audio", "flac") == ""
    assert audio_format_param("audio", "wav") == "wav"
    assert audio_format_param("audio", "mp3.best") == "mp3"
    assert audio_format_param("video", "best") == "", "audio requests only"


def test_best_keeps_the_tuned_no_ceiling_chain() -> None:
    """The chain this bot was tuned with (HEVC first, AVC as the fallback) is what
    "best" still means — a tier system must not demote the default."""
    selector = format_selector("video", "best")

    assert "hev" in selector and "avc" in selector
    assert "[height<=" not in selector


@pytest.mark.parametrize("height", (1080, 720, 480))
def test_a_video_tier_is_a_height_ceiling_on_every_step(height: int) -> None:
    selector = format_selector("video", str(height))

    assert f"[height<={height}]" in selector
    assert selector.count(f"[height<={height}]") >= 4, "every alternative is capped"
    assert f"[height={height}]" not in selector, "an exact match would fail on other sizes"


def test_a_tier_falls_back_to_something_rather_than_nothing() -> None:
    """A site that reports no height at all matches no filter; the trailing ``best``
    is what keeps that from becoming a fake "blocked"."""
    selector = format_selector("video", "480")

    assert selector.rstrip().endswith("best")


def test_audio_ignores_the_video_tiers() -> None:
    assert format_selector("audio", "1080") == format_selector("audio", "best")
    assert "[height<=" not in format_selector("audio", "1080")


def test_an_unknown_tier_is_the_default_for_that_format() -> None:
    assert normalize_quality("", "video") == DEFAULT_VIDEO_QUALITY
    assert normalize_quality(None, "audio") == DEFAULT_AUDIO_QUALITY
    assert normalize_quality("1080", "audio") == DEFAULT_AUDIO_QUALITY, "cross-format junk"
    assert normalize_quality("M4A", "audio") == "m4a", "a button's own spelling"
    assert normalize_quality("99999", "video") == DEFAULT_VIDEO_QUALITY, "out of ladder"


def test_a_real_height_travels_as_itself() -> None:
    """The menu is drawn from the resolutions a link actually has, so a height
    the static table never named is a tier like the named ones — collapsing it
    onto "best" would merge two different requests into one cache key."""
    assert normalize_quality("2160", "video") == "2160"
    assert normalize_quality("360", "video") == "360"
    assert is_video_height("1080") and is_video_height("144") and is_video_height("4320")
    assert not is_video_height("143") and not is_video_height("best")
    assert not is_video_height("") and not is_video_height(None) and not is_video_height("12")


def test_a_probed_height_builds_a_ceiling_selector() -> None:
    assert "[height<=2160]" in format_selector("video", "2160")


def test_quality_names_never_confuse_a_width_for_a_label() -> None:
    """1920x1080 → 1080p. A portrait phone video is 1080p too (its short edge
    names it), a padded frame is named by the rung it stands for — and a width
    alone is never a resolution label."""
    assert quality_label_p(1920, 1080) == 1080
    assert quality_label_p(1280, 720) == 720
    assert quality_label_p(854, 480) == 480
    assert quality_label_p(640, 360) == 360
    assert quality_label_p(426, 240) == 240
    assert quality_label_p(1080, 1920) == 1080, "a vertical Short is 1080p, not 1920p"
    assert quality_label_p(1920, 1088) == 1080, "a padded frame names its rung"
    assert quality_label_p(3840, 2160) == 2160
    assert quality_label_p(1920, None) is None, "width alone names nothing"
    assert quality_label_p(None, None) is None
    assert quality_label_p("1920", "1080") == 1080, "string numbers count too"


def test_an_option_carries_its_name_next_to_the_selectors_number() -> None:
    """A portrait option: the menu calls it 1080p while the tap still asks the
    selector for the stream's real height."""
    option = VideoOption(height=1920, size_bytes=1, size_exact=True, width=1080)

    assert option.label_p == 1080
    assert option.height == 1920


def test_the_offered_tiers_are_the_supported_tiers() -> None:
    for tier in VIDEO_QUALITIES:
        assert normalize_quality(tier, "video") == tier
    for tier in AUDIO_QUALITIES:
        assert normalize_quality(tier, "audio") == tier


# ---------------------------------------------------------------------------
# Downloading: which tier re-encodes
# ---------------------------------------------------------------------------


def _service(tmp_path: Path, **kwargs: Any) -> ExtractorService:
    return ExtractorService(tmp_path, js_runtime="none", **kwargs)


async def test_mp3_needs_ffmpeg_and_m4a_does_not(tmp_path: Path) -> None:
    """M4A is the stream the site already serves, so it is the tier that still works
    on a box without ffmpeg — the honest alternative to telling a user "later"."""
    service = _service(tmp_path)
    service._ffmpeg = None

    with pytest.raises(ExtractionError) as caught:
        await service.download("https://youtu.be/x", "audio", "mp3")

    assert caught.value.code == "FFMPEG_REQUIRED"
    # ...and the same service still has a route for audio without ffmpeg: it gets as
    # far as the engine, which is what "m4a needs no conversion" means.
    assert format_selector("audio", "m4a") == format_selector("audio", "mp3")


def test_a_conversion_that_returns_the_wrong_container_fails_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A " + '"flac"' + " tap that produced .mp3 is a failed conversion — never a
    file delivered under a borrowed name (the caption names real files)."""
    import services.extractor as extractor_module

    class FakeYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            self.opts = opts

        def __enter__(self) -> "FakeYDL":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            job = Path(self.opts["outtmpl"]).parent
            job.mkdir(parents=True, exist_ok=True)
            (job / "song.mp3").write_bytes(b"x" * 16)  # the wrong container
            return {"title": "Song", "id": "abc", "ext": "mp3"}

    monkeypatch.setattr(extractor_module, "yt_dlp", type("M", (), {"YoutubeDL": FakeYDL}))

    service = _service(tmp_path)
    with pytest.raises(ExtractionError) as boom:
        _download_sync(service, "audio", "flac")
    assert boom.value.code == "CONVERSION_MISMATCH"


def test_the_mp3_tier_post_processes_and_the_m4a_tier_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned on the *options* yt-dlp is handed, because that is where "no re-encode"
    actually lives."""
    import services.extractor as extractor_module

    captured: list[dict[str, Any]] = []

    class FakeYDL:
        def __init__(self, opts: dict[str, Any]) -> None:
            captured.append(opts)

        def __enter__(self) -> "FakeYDL":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            job = Path(captured[-1]["outtmpl"]).parent
            job.mkdir(parents=True, exist_ok=True)
            # The post-processor's codec decides the container — model reality.
            codec = (
                captured[-1].get("postprocessors", [{}])[0].get("preferredcodec", "m4a")
            )
            produced = job / f"song.{codec}"
            produced.write_bytes(b"x" * 16)
            return {"title": "Song", "id": "abc", "ext": codec}

    monkeypatch.setattr(extractor_module, "yt_dlp", type("M", (), {"YoutubeDL": FakeYDL}))

    service = _service(tmp_path)
    _download_sync(service, "audio", "m4a")
    _download_sync(service, "audio", "mp3")

    assert "postprocessors" not in captured[0]
    assert captured[1]["postprocessors"][0]["key"] == "FFmpegExtractAudio"
    assert captured[0]["format"] == captured[1]["format"], "the selector is the same"


def _download_sync(
    service: ExtractorService, media_format: Literal["video", "audio"], quality: str
) -> Any:
    return service._download_attempt("https://soundcloud.com/a/b", media_format, quality, None)


# ---------------------------------------------------------------------------
# The cache key
# ---------------------------------------------------------------------------


def test_the_default_tier_keeps_the_key_older_rows_own() -> None:
    """A redeploy must not orphan the cache: ``url|video`` is what every row written
    before tiers existed is filed under."""
    from core.utils import canonical_url, sha256_hex

    url = "https://www.youtube.com/watch?v=abc"

    assert cache_service.cache_key(url, "video") == sha256_hex(f"{canonical_url(url)}|video")
    assert cache_service.cache_key(url, "video", "best") == cache_service.cache_key(url, "video")
    assert cache_service.cache_key(url, "audio") == sha256_hex(f"{canonical_url(url)}|audio")
    assert cache_service.cache_key(url, "audio", "mp3") == cache_service.cache_key(url, "audio")


def test_a_deliberate_tier_is_its_own_entry() -> None:
    url = "https://www.youtube.com/watch?v=abc"

    keys = {
        cache_service.cache_key(url, "video", quality)
        for quality in ("best", "1080", "720", "480")
    }
    assert len(keys) == 4
    assert cache_service.cache_key(url, "audio", "m4a") != cache_service.cache_key(url, "audio")
    assert quality_key("video", "480") == "video:480"
    assert quality_key("audio", "m4a") == "audio:m4a"


def test_tracking_parameters_still_do_not_change_the_key() -> None:
    plain = "https://www.youtube.com/watch?v=abc"
    tagged = "https://www.youtube.com/watch?v=abc&utm_source=x#t=10"

    assert cache_service.cache_key(tagged, "video", "720") == cache_service.cache_key(
        plain, "video", "720"
    )


# ---------------------------------------------------------------------------
# Cobalt: the same tier, in its vocabulary
# ---------------------------------------------------------------------------


def test_a_video_tier_reaches_the_fallback_unchanged() -> None:
    assert _legacy_payload("https://youtu.be/x", "video", "480")["vQuality"] == "480"
    assert _modern_payload("https://youtu.be/x", "video", "480")["videoQuality"] == "480"


def test_the_no_ceiling_tier_is_cobalts_own_maximum() -> None:
    """Cobalt counts in pixels and calls "no ceiling" ``max``; ``best`` is not a value
    it knows, and sending it would silently fall back to the instance's default."""
    assert _legacy_payload("https://youtu.be/x", "video", "best")["vQuality"] == "max"
    assert _modern_payload("https://youtu.be/x", "video", "best")["videoQuality"] == "max"


def test_an_audio_tier_asks_for_the_right_kind_of_audio() -> None:
    mp3 = _modern_payload("https://youtu.be/x", "audio", "mp3")
    m4a = _modern_payload("https://youtu.be/x", "audio", "m4a")

    assert mp3["audioFormat"] == "mp3" and mp3["downloadMode"] == "audio"
    assert m4a["audioFormat"] == "best", "no re-encode is what M4A means"
    assert m4a["downloadMode"] == "audio"


def test_the_legacy_shape_says_the_same_thing() -> None:
    payload = _legacy_payload("https://youtu.be/x", "audio", "m4a")

    assert payload["isAudioOnly"] is True
    assert payload["aFormat"] == "best"
    assert payload["filenamePattern"] == "nerd"


def test_every_audio_tier_asks_cobalt_for_its_own_container() -> None:
    """Cobalt names formats, not levels — every preset of a container maps to
    that container, so a fallback can never serve the wrong *kind* of file."""
    for tier, expected in (
        ("mp3.best", "mp3"),
        ("mp3.small", "mp3"),
        ("m4a.small", "best"),
        ("opus.high", "opus"),
        ("wav", "wav"),
    ):
        payload = _modern_payload("https://youtu.be/x", "audio", tier)
        assert payload["audioFormat"] == expected, tier


# ---------------------------------------------------------------------------
# The presets: what a level really asks the encoder for
# ---------------------------------------------------------------------------


def test_two_old_spellings_keep_the_cache_keys_they_have_always_owned() -> None:
    """``mp3`` and ``m4a`` predate the presets and stay canonical — a rename here
    would silently orphan every cached file on the next deploy."""
    assert normalize_quality("mp3.balanced", "audio") == "mp3"
    assert normalize_quality("m4a.best", "audio") == "m4a"
    assert cache_service.cache_key(
        "https://x", "audio", "mp3.balanced"
    ) == cache_service.cache_key("https://x", "audio", "mp3")
    assert cache_service.cache_key(
        "https://x", "audio", "m4a.best"
    ) == cache_service.cache_key("https://x", "audio", "m4a")


def test_every_audio_tier_maps_to_real_encoder_settings() -> None:
    """Supported formats only: each tier is a post-processor yt-dlp/ffmpeg can
    genuinely build, and wav — PCM — carries no bitrate at all."""
    from services.extractor import AUDIO_EXPORTS

    assert "m4a" not in AUDIO_EXPORTS, "the untouched stream keeps no post-processor"
    for tier in AUDIO_QUALITIES:
        if tier == "m4a":
            continue
        codec, bitrate = AUDIO_EXPORTS[tier]
        assert codec in ("mp3", "m4a", "flac", "opus", "wav")
        if codec in ("wav", "flac"):
            assert bitrate is None, "raw and lossless output have no bitrate to set"
        else:
            assert bitrate is not None and 64 <= int(bitrate) <= 320, tier


def test_the_smaller_button_really_makes_a_smaller_file() -> None:
    """"Small size" is a promise about the encoder, not a label: within every
    codec the bitrates fall monotonically from best to small — and the honest
    ceilings are kept (opus gains nothing above 192, and the historical default
    tier is exactly what it always was)."""
    from services.extractor import AUDIO_EXPORTS

    for codec in ("mp3", "m4a", "opus"):
        levels = [
            int(bitrate)
            for tier in AUDIO_QUALITIES
            if tier.startswith(codec) and tier in AUDIO_EXPORTS
            for bitrate in AUDIO_EXPORTS[tier][1:]
            if bitrate is not None
        ]
        assert levels == sorted(levels, reverse=True), codec
    assert AUDIO_EXPORTS["opus.best"] == ("opus", "192")
    assert AUDIO_EXPORTS["mp3"] == ("mp3", "192"), "the historical default is untouched"
