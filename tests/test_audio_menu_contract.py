"""The audio menu/delivery contract: encoding targets below a stated source ceiling.

The invariant (the audio half of the menu/delivery contract):

    an audio quality row must not promise a source quality that the production
    pipeline cannot honestly support.

The project's UI semantics — established by reading the screens, not assumed —
are the two-part model, already implemented:

* **output encoding target** — a level row's ``N kbps`` is what the file will be
  *encoded* at (``handlers.user._level_rows``: "the number is what the file will
  be"), and the delivered caption names the same rate
  (``services.delivery.quality_label``);
* **source quality ceiling** — re-encodes *above* the source's own rate are not
  advertised at all (``_level_rows.honest``), and the source's rate is stated on
  the screen whenever it is known (``_source_rate_note``), so no row silently
  implies information the source does not contain.

A transcoder can technically emit 320 kbps MP3 from 128 kbps AAC — and the menu
simply will not offer it. These tests pin both halves, offline.
"""

from __future__ import annotations

import re

from core.utils import AUDIO_TIERS
from handlers.user import _level_rows, _source_rate_note
from services.content import audio_tier_levels
from services.delivery import quality_label
from services.extractor import _CODEC_EXT, AUDIO_EXPORTS, audio_bitrate

EN = "en"


def _rates(codec: str = "mp3", **kwargs: object) -> list[int | None]:
    """The kbps each shown row names (``None`` = the no-re-encode "Original" row)."""
    out: list[int | None] = []
    for label, _callback in _level_rows(codec, EN, **kwargs):  # type: ignore[arg-type]
        match = re.search(r"(\d+) kbps", label)
        out.append(int(match.group(1)) if match else None)
    return out


# 1. A source at or above 320 kbps → the 320 kbps row may be advertised.
def test_source_at_or_above_320_keeps_the_full_ladder() -> None:
    assert _rates(source_kbps=320) == [320, 256, 192, 128]
    assert _rates(source_kbps=512) == [320, 256, 192, 128]


# 2. A source around 130 kbps → 320/256/192 are never offered above the source.
def test_source_around_130_advertises_no_upscale_rows() -> None:
    rows = _rates(source_kbps=130)
    assert rows == [128]
    for hidden in (320, 256, 192):
        assert hidden not in rows


def test_original_stream_row_survives_the_source_ceiling() -> None:
    # The untouched source stream *is* the source — it can never exceed itself.
    assert _rates("m4a", source_kbps=130) == [None, 128]


# 3. A source around 192 kbps → exactly 192 and below.
def test_source_around_192_keeps_exactly_192_and_below() -> None:
    assert _rates(source_kbps=192) == [192, 128]


# 4. A source at 128 kbps → 128 remains valid.
def test_source_128_keeps_128() -> None:
    assert _rates(source_kbps=128) == [128]


# 5. The encoder could produce more; the menu must not imply more.
def test_rows_are_encoding_targets_and_the_source_ceiling_is_stated() -> None:
    # Every shown row names exactly its encoding target (never an aspiration).
    for label, callback in _level_rows("mp3", EN, source_kbps=320):
        tier = callback.rsplit(":", 1)[-1]
        assert f"{audio_bitrate(tier)} kbps" in label
    # The ceiling is stated whenever it is known — trimmed ladder or not …
    assert _source_rate_note("mp3", {"source_kbps": 130}, EN) == "ℹ️ Source quality: 130 kbps"
    assert _source_rate_note("mp3", {"source_kbps": 130, "source_kbps_approx": True}, EN) == (
        "ℹ️ Source quality: ~130 kbps"
    )
    # … and a source that said nothing stays silent rather than invented.
    assert _source_rate_note("mp3", {"source_kbps": None}, EN) == ""
    # Even the floor row (an emptied ladder keeps its smallest row) never speaks
    # without the ceiling beside it.
    assert _rates(source_kbps=100) == [128]
    assert _source_rate_note("mp3", {"source_kbps": 100}, EN) != ""


def test_lossless_containers_name_their_container_and_promise_no_rate() -> None:
    # FLAC/WAV have no knob: the caption names the container — "no knob, no claim".
    flac = quality_label("audio", "flac", EN)
    wav = quality_label("audio", "wav", EN)
    assert "FLAC" in flac and re.search(r"\d+ kbps", flac) is None
    assert "WAV" in wav and re.search(r"\d+ kbps", wav) is None
    # …while a re-encode tier names its real rate on the caption too.
    assert "320 kbps" in quality_label("audio", "mp3.best", EN)


# 6. Tier vocabulary and conversion specs are unchanged.
def test_tier_names_and_conversion_behavior_are_unchanged() -> None:
    assert audio_tier_levels("mp3") == (
        ("best", "mp3.best"),
        ("high", "mp3.high"),
        ("balanced", "mp3"),
        ("small", "mp3.small"),
    )
    assert {tier: audio_bitrate(tier) for tier in ("mp3.best", "mp3.high", "mp3", "mp3.small")} == {
        "mp3.best": 320,
        "mp3.high": 256,
        "mp3": 192,
        "mp3.small": 128,
    }
    assert AUDIO_TIERS[("mp3", "best")] == "mp3.best"
    assert AUDIO_EXPORTS["mp3.best"] == ("mp3", "320")
    assert AUDIO_EXPORTS["mp3.high"] == ("mp3", "256")
    assert AUDIO_EXPORTS["mp3"] == ("mp3", "192")
    assert AUDIO_EXPORTS["mp3.small"] == ("mp3", "128")
    assert AUDIO_EXPORTS["flac"] == ("flac", None)
    assert AUDIO_EXPORTS["wav"] == ("wav", None)
    assert _CODEC_EXT["mp3"] == ".mp3" and _CODEC_EXT["flac"] == ".flac"
