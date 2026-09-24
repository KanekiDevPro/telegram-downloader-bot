"""Recorded-ladder smoke: the menu's rungs must equal the chain's delivery.

A small, deterministic tripwire built from the ladder Live-tap A *measured* on a
real YouTube source (real itags, codecs, rates, sizes). Its one job: future
edits — codec preference, ``format_sort``, ``_video_selector``, menu
availability logic — must not silently reopen the gap where the menu advertised
1440p/2160p and every tap arrived as 1080p.

Two recorded shapes:

* **Fixture A — current YouTube shape**: H.264 through 1080p, VP9 (and nothing
  the chain prefers) above it. 360/480/720/1080 advertise and deliver; 1440 and
  2160 exist but must stay unadvertised because the chain would deliver 1080.
* **Fixture B — compatible high-resolution codecs**: the same ladder plus the
  legacy H.264 1440p/2160p itags (264/266). 1440 and 2160 advertise and
  deliver at full resolution.

Offline: no YouTube request anywhere.
"""

from __future__ import annotations

from typing import Any

from services.extractor import selected_streams, video_options


def _stream(
    format_id: str,
    height: int,
    width: int,
    vcodec: str,
    *,
    ext: str = "mp4",
    fps: int = 30,
    tbr: float,
    size: int,
) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "ext": ext,
        "width": width,
        "height": height,
        "fps": fps,
        "vcodec": vcodec,
        "acodec": "none",
        "tbr": tbr,
        "filesize": size,
        "protocol": "https",
        "url": f"https://example.invalid/{format_id}",
    }


#: Recorded from Live-tap A (aqz-KE-bpKQ, 2026-09-24) — one representative
#: stream per (height, codec family), real itags and rates.
FIXTURE_A: list[dict[str, Any]] = [
    _stream("134", 360, 640, "avc1.4d401e", tbr=230.634, size=18_294_110),
    _stream("243", 360, 640, "vp9", ext="webm", tbr=303.949, size=24_109_536),
    _stream("135", 480, 854, "avc1.4d401f", tbr=355.608, size=28_207_144),
    _stream("244", 480, 854, "vp9", ext="webm", tbr=417.772, size=33_138_062),
    _stream("136", 720, 1280, "avc1.4d4020", tbr=1_100.517, size=87_293_859),
    _stream("298", 720, 1280, "avc1.4d4020", fps=60, tbr=1_897.673, size=150_524_867),
    _stream("302", 720, 1280, "vp9", ext="webm", fps=60, tbr=1_420.515, size=112_676_322),
    _stream("299", 1080, 1920, "avc1.64002a", fps=60, tbr=3_247.821, size=257_619_653),
    _stream("303", 1080, 1920, "vp9", ext="webm", fps=60, tbr=2_127.264, size=168_736_189),
    _stream("308", 1440, 2560, "vp9", ext="webm", fps=60, tbr=5_967.715, size=473_363_704),
    _stream("315", 2160, 3840, "vp9", ext="webm", fps=60, tbr=17_174.188, size=1_362_269_481),
    {
        "format_id": "140",
        "ext": "m4a",
        "width": None,
        "height": None,
        "vcodec": "none",
        "acodec": "mp4a.40.2",
        "tbr": 129.481,
        "filesize": 10_271_496,
        "protocol": "https",
        "url": "https://example.invalid/140",
    },
]

#: Fixture A plus the legacy H.264 high-resolution itags (264 = 1440p,
#: 266 = 2160p) — the "compatible codec at the requested rung" shape.
FIXTURE_B = FIXTURE_A + [
    _stream("264", 1440, 2560, "avc1.640032", fps=30, tbr=5_964.163, size=473_363_704),
    _stream("266", 2160, 3840, "avc1.640032", fps=30, tbr=17_174.188, size=1_362_269_481),
]


def _info(formats: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": "recorded", "title": "recorded", "duration": 635, "formats": formats}


def _delivered(formats: list[dict[str, Any]], height: int) -> dict[str, Any]:
    streams = selected_streams(_info(formats), height)
    return next(f for f in streams if f.get("vcodec") not in (None, "none"))


def test_fixture_a_advertises_exactly_the_chain_deliverable_rungs() -> None:
    options = video_options(_info(FIXTURE_A))
    assert [option.height for option in options] == [1080, 720, 480, 360]


def test_fixture_a_chain_picks_match_the_live_tap() -> None:
    # The exact rungs Live-tap A measured — a codec-preference, sort or
    # `_video_selector` change moves at least one of these picks.
    expected = {360: "134", 480: "135", 720: "298", 1080: "299"}
    for height, format_id in expected.items():
        assert _delivered(FIXTURE_A, height)["format_id"] == format_id


def test_fixture_a_high_rungs_stay_unadvertised_while_a_tap_lands_on_1080() -> None:
    labels = {option.height for option in video_options(_info(FIXTURE_A))}
    assert 1440 not in labels and 2160 not in labels
    # The measured reason, kept visible: those requests really do land on 299.
    assert _delivered(FIXTURE_A, 1440)["format_id"] == "299"
    assert _delivered(FIXTURE_A, 2160)["format_id"] == "299"


def test_fixture_b_advertises_and_delivers_1440_and_2160() -> None:
    options = video_options(_info(FIXTURE_B))
    labels = {option.height for option in options}
    assert {1440, 2160} <= labels
    assert _delivered(FIXTURE_B, 1440)["format_id"] == "264"
    assert _delivered(FIXTURE_B, 2160)["format_id"] == "266"


def test_no_advertised_rung_ever_exceeds_its_delivery() -> None:
    # The tripwire itself: advertised_height > delivered_height fails the smoke.
    for formats in (FIXTURE_A, FIXTURE_B):
        for option in video_options(_info(formats)):
            delivered = _delivered(formats, option.height)
            assert delivered["height"] >= option.height, (
                f"menu advertises {option.height}p but a tap delivers "
                f"{delivered['height']}p ({delivered['format_id']})"
            )
            assert delivered["height"] == option.height
