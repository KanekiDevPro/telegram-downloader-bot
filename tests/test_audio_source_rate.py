"""The source's own audio rate, and what it does to the bitrate menu.

Re-encoding a 128 kbps source at 320 kbps is a bigger file, not better sound —
so when the source's rate is *known*, the re-encodes above it vanish from the
menu and one ℹ️ line says why. When it is not known, the ladder stands whole and
nothing claims otherwise. Every case here pins one of those two honest halves.
"""
from __future__ import annotations

import re
from typing import Any

from core.i18n import t
from handlers.user import _level_rows, _marks, _source_rate_note
from services.extractor import _audio_rate_kbps

EN = "en"
FA = "fa"

#: An audio-only stream (the one an audio request would actually fetch).
AUDIO_ONLY = {"acodec": "opus", "vcodec": "none"}
#: A muxed stream: its ``tbr`` is video+audio together and must never be read.
MUXED = {"acodec": "mp4a.40.2", "vcodec": "avc1"}
#: A video-only stream says nothing about audio at all.
VIDEO_ONLY = {"acodec": "none", "vcodec": "vp9"}


def _info(*streams: dict[str, Any]) -> dict[str, Any]:
    return {"formats": list(streams)}


def _rates(rows: list[tuple[str, str]]) -> list[int]:
    """The kbps numbers of the rows, in order."""
    found = []
    for label, _data in rows:
        match = re.search(r"(\d+) kbps", label)
        if match:
            found.append(int(match.group(1)))
    return found


# ---------------------------------------------------------------------------
# Probing the source
# ---------------------------------------------------------------------------


def test_the_source_rate_is_read_from_the_audio_stream() -> None:
    """The stream's own declared ``abr`` is the exact number, not an estimate."""
    assert _audio_rate_kbps(_info({**AUDIO_ONLY, "abr": 128})) == (128, False)


def test_a_measured_rate_is_marked_as_an_estimate() -> None:
    """``tbr`` is measured — the caller may show it with a ``~``, never bare."""
    assert _audio_rate_kbps(_info({**AUDIO_ONLY, "tbr": 127.7})) == (128, True)


def test_a_muxed_streams_total_bitrate_is_never_the_audio_rate() -> None:
    """A video's ``tbr`` is video+audio together: reading it would cap by a lie."""
    assert _audio_rate_kbps(_info({**MUXED, "tbr": 2500})) == (None, False)


def test_video_only_streams_say_nothing_about_audio() -> None:
    assert _audio_rate_kbps(_info({**VIDEO_ONLY, "tbr": 900})) == (None, False)


def test_the_highest_audio_only_stream_is_the_source_rate() -> None:
    """Several audio streams: the best of them is what a re-encode competes with."""
    streams = _info({**AUDIO_ONLY, "abr": 70}, {**AUDIO_ONLY, "abr": 128})
    assert _audio_rate_kbps(streams) == (128, False)


def test_a_rate_read_from_a_measurement_stays_approximate() -> None:
    """Exact and measured streams together: the flag follows the number chosen."""
    streams = _info({**AUDIO_ONLY, "abr": 70}, {**AUDIO_ONLY, "tbr": 128})
    assert _audio_rate_kbps(streams) == (128, True)


def test_an_undeclared_rate_stays_unknown() -> None:
    """Nothing declared → unknown — and unknown must never become a guess."""
    assert _audio_rate_kbps(_info()) == (None, False)
    assert _audio_rate_kbps(_info({**AUDIO_ONLY})) == (None, False)
    assert _audio_rate_kbps(_info({**AUDIO_ONLY, "abr": 0})) == (None, False)


# ---------------------------------------------------------------------------
# What the menu does with it
# ---------------------------------------------------------------------------


def test_a_known_source_hides_the_big_re_encodes() -> None:
    """A 128 kbps source: only 128 is offered — 320 would be a bigger file, no more."""
    rows = _level_rows("mp3", EN, duration=150, source_kbps=128)
    assert _rates(rows) == [128]
    assert rows[0][0].startswith("💎 128 kbps")


def test_a_source_between_presets_keeps_the_honest_neighbours() -> None:
    rows = _level_rows("mp3", EN, duration=150, source_kbps=250)
    assert _rates(rows) == [192, 128]
    assert [row[0].split(" ", 1)[0] for row in rows] == ["💎", "📦"]


def test_a_source_above_every_preset_keeps_the_whole_ladder() -> None:
    rows = _level_rows("mp3", EN, duration=150, source_kbps=500)
    assert _rates(rows) == [320, 256, 192, 128]


def test_a_tiny_source_keeps_its_floor_instead_of_an_empty_menu() -> None:
    """Below every preset: the least misleading row left, never an empty screen."""
    rows = _level_rows("mp3", EN, duration=150, source_kbps=64)
    assert _rates(rows) == [128]


def test_an_unknown_source_never_trims_the_ladder() -> None:
    """Unknown source rate → the whole ladder, and no ℹ️ claiming to know better."""
    rows = _level_rows("mp3", EN, duration=150)
    assert _rates(rows) == [320, 256, 192, 128]
    assert _source_rate_note("mp3", {}, FA) == ""


def test_no_row_promises_more_than_the_source_delivers() -> None:
    rows = _level_rows("mp3", EN, duration=150, source_kbps=128)
    assert all(rate <= 128 for rate in _rates(rows))


def test_a_format_whose_floor_is_the_source_shows_exactly_that() -> None:
    """OPUS bottoms out at 64 — a 64 kbps source offers that one row."""
    rows = _level_rows("opus", EN, duration=150, source_kbps=64)
    assert _rates(rows) == [64]


def test_the_untouched_stream_survives_the_source_cap() -> None:
    """The original stream *is* the source — never hidden, never given a ceiling.

    A 128 kbps re-encode equals the source and stays (it costs nothing to
    offer); the bigger ones are gone.
    """
    rows = _level_rows("m4a", EN, duration=150, source_kbps=128)
    labels = [row[0] for row in rows]
    assert labels[0].startswith("💎")
    assert t("audio.original_long", EN) in labels[0]
    assert _rates(rows[1:]) == [128]


def test_the_note_only_speaks_when_something_was_hidden() -> None:
    hidden = _source_rate_note("mp3", {"source_kbps": 128}, FA)
    assert "128 kbps" in hidden
    assert hidden == t("audio.source_rate", FA, rate="128 kbps")
    # Nothing hidden (a 320 source) → nothing to explain.
    assert _source_rate_note("mp3", {"source_kbps": 320}, FA) == ""


def test_an_approximate_source_rate_says_so() -> None:
    note = _source_rate_note("mp3", {"source_kbps": 128, "source_kbps_approx": True}, FA)
    assert note == t("audio.source_rate", FA, rate="~128 kbps")


def test_the_marks_rank_the_rows_that_are_shown() -> None:
    assert _marks(1) == ("💎",)
    assert _marks(2) == ("💎", "📦")
    assert _marks(3) == ("💎", "⚖️", "📦")
    assert _marks(4) == ("💎", "🔥", "⚖️", "📦")
