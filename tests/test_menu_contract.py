"""The menu/delivery contract: an advertised rung must be the rung a tap delivers.

Live-tap A caught the gap these tests close: the quality menu advertised 1440p
and 2160p (with sizes!) on a YouTube source whose streams at those heights are
VP9/AV1-only, while the production format chain — H.265, then H.264, then
whatever remains — resolved every such request to 1080p H.264. The chain is not
wrong to prefer Telegram-friendly codecs (it delivers 1440p/2160p just fine when
H.264/HEVC exists there); the menu was wrong to promise rungs the chain would
trade down.

The contract, pinned here at the selector/menu boundary with no network:

    for every row ``video_options`` advertises, requesting that row's height
    through the production chain lands on the rung the row names.

Synthetic ladders mirror the three shapes the live survey found: mixed
(H.264 ≤1080p with VP9/AV1 above), and the same ladders with H.264 or HEVC
present at 1440p/2160p.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.extractor import (
    VideoOption,
    quality_label_p,
    selected_streams,
    video_options,
)


def _stream(
    format_id: str,
    height: int,
    vcodec: str,
    *,
    tbr: float = 800.0,
    size: int = 10_000_000,
    exact: bool = True,
    ext: str = "mp4",
) -> dict[str, Any]:
    """One silent (video-only) stream, shaped like yt-dlp's DASH entries."""
    return {
        "format_id": format_id,
        "ext": ext,
        "width": height * 16 // 9,
        "height": height,
        "fps": 60,
        "vcodec": vcodec,
        "acodec": "none",
        "tbr": tbr,
        "protocol": "https",
        "url": f"https://example.invalid/{format_id}",
        **({"filesize": size} if exact else {"filesize_approx": size}),
    }


def _audio(format_id: str = "140", size: int = 12_000_000) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "ext": "m4a",
        "width": None,
        "height": None,
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "tbr": 128.0,
        "filesize": size,
        "protocol": "https",
        "url": f"https://example.invalid/{format_id}",
    }


#: The Live-tap A shape: H.264 up to 1080p, 1440p/2160p in VP9 and AV1 only —
#: the ladder every real YouTube source showed in the 23-ladder live survey.
MIXED: list[dict[str, Any]] = [
    _stream("134", 360, "avc1.4d401e", tbr=230.0, size=18_294_110),
    _stream("135", 480, "avc1.4d401f", tbr=355.0, size=28_207_144),
    _stream("136", 720, "avc1.4d401f", tbr=1_100.0, size=87_293_859),
    _stream("299", 1080, "avc1.64002a", tbr=3_247.0, size=257_619_653),
    _stream("308", 1440, "vp9", ext="webm", tbr=5_967.0, size=473_363_704),
    _stream("315", 2160, "vp9", ext="webm", tbr=17_174.0, size=1_362_269_481),
    _audio(),
]

#: The same ladder with HEVC at 1440p/2160p — what the chain prefers most.
HEVC_HIGH = MIXED + [
    _stream("he10", 1440, "hev1.2.4.L123", tbr=2_500.0, size=200_000_000),
    _stream("he20", 2160, "hev1.2.4.L153", tbr=5_000.0, size=400_000_000),
]

#: The same ladder with H.264 at 1440p/2160p — the second-preferred codec.
AVC_HIGH = MIXED + [
    _stream("av14", 1440, "avc1.640032", tbr=2_500.0, size=200_000_000),
    _stream("av21", 2160, "avc1.640032", tbr=5_000.0, size=400_000_000),
]


def _info(formats: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "synthetic", "title": "synthetic", "duration": 600, "formats": formats}


def _video_part(streams: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    video = next(f for f in streams if f.get("vcodec") not in (None, "none"))
    return video


def _delivered_label(formats: list[dict[str, Any]], height: int) -> int:
    """The rung name a tap on ``height`` actually arrives as."""
    video = _video_part(selected_streams(_info(formats), height))
    return quality_label_p(video.get("width"), video.get("height")) or int(video["height"])


def _labels(options: tuple[VideoOption, ...]) -> list[int]:
    return [option.label_p for option in options]


# 1. 1080p available → 1080p advertised and delivered.
def test_1080p_is_advertised_and_delivered() -> None:
    options = video_options(_info(MIXED))
    assert 1080 in _labels(options)
    row = next(o for o in options if o.label_p == 1080)
    assert _delivered_label(MIXED, row.height) == 1080


# 2. 1440p in H.264 or HEVC → 1440p advertised and delivered.
@pytest.mark.parametrize(
    ("formats", "codec_prefix"),
    [(HEVC_HIGH, "hev"), (AVC_HIGH, "avc")],
    ids=["hevc-1440", "h264-1440"],
)
def test_1440p_with_compatible_codec_is_advertised_and_delivered(
    formats: list[dict[str, Any]], codec_prefix: str
) -> None:
    options = video_options(_info(formats))
    assert 1440 in _labels(options)
    row = next(o for o in options if o.label_p == 1440)
    assert _delivered_label(formats, row.height) == 1440
    video = _video_part(selected_streams(_info(formats), row.height))
    assert str(video["vcodec"]).startswith(codec_prefix)


# 3. 2160p in H.264 or HEVC → 2160p advertised and delivered.
@pytest.mark.parametrize(
    ("formats", "codec_prefix"),
    [(HEVC_HIGH, "hev"), (AVC_HIGH, "avc")],
    ids=["hevc-2160", "h264-2160"],
)
def test_2160p_with_compatible_codec_is_advertised_and_delivered(
    formats: list[dict[str, Any]], codec_prefix: str
) -> None:
    options = video_options(_info(formats))
    assert 2160 in _labels(options)
    row = next(o for o in options if o.label_p == 2160)
    assert _delivered_label(formats, row.height) == 2160
    video = _video_part(selected_streams(_info(formats), row.height))
    assert str(video["vcodec"]).startswith(codec_prefix)


# 4. 1440p only as a codec the chain will not select there → not advertised.
def test_1440p_only_as_unselected_codec_is_not_advertised() -> None:
    options = video_options(_info(MIXED))
    assert 1440 not in _labels(options)
    # The documented reason, measured through the same chain: the request lands
    # on the 1080p H.264 rung instead — a delivery the menu must not promise past.
    assert _delivered_label(MIXED, 1440) == 1080


# 5. 2160p only as a codec the chain will not select there → not advertised.
def test_2160p_only_as_unselected_codec_is_not_advertised() -> None:
    options = video_options(_info(MIXED))
    assert 2160 not in _labels(options)
    assert _delivered_label(MIXED, 2160) == 1080


# 6. No advertised rung may silently resolve to a different (lower) rung.
@pytest.mark.parametrize(
    "formats", [MIXED, HEVC_HIGH, AVC_HIGH], ids=["mixed", "hevc-high", "avc-high"]
)
def test_no_advertised_rung_silently_resolves_elsewhere(formats: list[dict[str, Any]]) -> None:
    for option in video_options(_info(formats)):
        assert _delivered_label(formats, option.height) == option.label_p, (
            f"the menu advertises {option.label_p}p (height {option.height}) but a tap "
            "delivers another rung"
        )


def test_mixed_ladder_never_claims_above_1080p_while_a_tap_would_land_there() -> None:
    # The exact Live-tap A regression: menu claims 2160p, tap resolves to 1080p.
    options = video_options(_info(MIXED))
    assert max(_labels(options)) == 1080
    assert [str(o.height) for o in options] == ["1080", "720", "480", "360"]


# 7. The classic ladder keeps its rows, heights, and sizes exactly as before.
def test_360_480_720_1080_behavior_is_preserved() -> None:
    options = video_options(_info(MIXED))
    assert options == (
        VideoOption(1080, 257_619_653 + 12_000_000, True, 1920),
        VideoOption(720, 87_293_859 + 12_000_000, True, 1280),
        VideoOption(480, 28_207_144 + 12_000_000, True, 853),
        VideoOption(360, 18_294_110 + 12_000_000, True, 640),
    )
