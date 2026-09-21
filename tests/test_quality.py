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
    AUDIO_QUALITIES,
    DEFAULT_AUDIO_QUALITY,
    DEFAULT_VIDEO_QUALITY,
    VIDEO_QUALITIES,
    normalize_quality,
    quality_key,
)
from services import cache as cache_service
from services.cobalt import _legacy_payload, _modern_payload
from services.extractor import ExtractionError, ExtractorService, format_selector

# ---------------------------------------------------------------------------
# yt-dlp: what a tier means
# ---------------------------------------------------------------------------


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
    assert normalize_quality("2160", "video") == DEFAULT_VIDEO_QUALITY
    assert normalize_quality("", "video") == DEFAULT_VIDEO_QUALITY
    assert normalize_quality(None, "audio") == DEFAULT_AUDIO_QUALITY
    assert normalize_quality("1080", "audio") == DEFAULT_AUDIO_QUALITY, "cross-format junk"
    assert normalize_quality("M4A", "audio") == "m4a", "a button's own spelling"


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
            produced = job / "song.m4a"
            produced.write_bytes(b"x" * 16)
            return {"title": "Song", "id": "abc", "ext": "m4a"}

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
