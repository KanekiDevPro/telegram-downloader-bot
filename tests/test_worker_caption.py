"""The media card: 🎬 title, 🔗 link, 🎞 quality, 🤖 bot — the caption contract.

The screen that asks and the caption that arrives are the same block (see
``services/delivery.py:media_card``), so these pin what every downloadable file
says about itself: the four lines, their order, what each carries — and what is
*omitted* when a fact is missing, because «None», «Unknown» and an empty field
are the three things a card must never show.

The 🎞 line is where a quality tier becomes honest: the menu offers ceilings and
real resolutions, and the caption names the resolution the delivered file
actually has. A song is captioned as the song (🎵 its title, 🎤 its credits),
never as the plumbing that fetched it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest

from core.i18n import t
from services import delivery
from services import spotify as spotify_module
from services.extractor import DownloadResult, MediaInfo
from services.worker import _upload_caption

FA = "fa"
EN = "en"
BOT = "AnimStoreV2ray_bot"
URL = "https://youtu.be/abc"


@pytest.fixture(autouse=True)
def _booted_bot() -> Any:
    """A bot with a known @handle — and a clean module state afterwards."""
    delivery.set_bot_username(BOT)
    yield
    delivery.set_bot_username("")


def _result(
    tmp_path: Path,
    *,
    title: str = "A Clip",
    height: int | None = None,
    label_p: int | None = None,
    quality: str = "",
    media_format: str = "video",
    source_url: str = "",
) -> DownloadResult:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x" * 2048)
    info = MediaInfo(
        source_url=URL,
        title=title,
        platform="youtube",
        webpage_url=URL,
        extension="mp4",
        thumbnail=None,
        duration=95,
        filesize_approx=2048,
        is_live=False,
        height=height,
        label_p=label_p,
    )
    return DownloadResult(
        file_path=media,
        info=info,
        media_format=media_format,  # type: ignore[arg-type]
        quality=quality,
    )


# ---------------------------------------------------------------------------
# The four lines
# ---------------------------------------------------------------------------


def test_the_card_is_four_lines_and_no_filler(tmp_path: Path) -> None:
    caption = _upload_caption(
        _result(tmp_path, height=1080), EN, source_url=URL
    )

    assert caption.splitlines() == [
        "🎬 A Clip",
        f"🔗 {URL}",
        "🎞 1080p • 2.0 KB",
        f"🤖 @{BOT}",
    ]
    # Platform and length are facts the file already speaks for itself.
    assert "🌐" not in caption and "📦" not in caption and "⏱" not in caption


def test_the_quality_line_uses_the_conventional_name(tmp_path: Path) -> None:
    """1920x1080 is 1080p — a pixel width or a portrait's long edge is never a
    resolution label."""
    caption = _upload_caption(
        _result(tmp_path, height=1920, label_p=1080), EN, source_url=URL
    )

    assert "🎞 1080p • 2.0 KB" in caption
    assert "1920p" not in caption


def test_the_quality_line_names_the_real_resolution_not_the_tier(tmp_path: Path) -> None:
    """A 720p file answering a "1080p" tap says 720p — the honest version of the
    ceiling the button promised."""

    caption = _upload_caption(
        _result(tmp_path, height=720, quality="1080"), EN, source_url=URL
    )

    assert "🎞 720p" in caption
    assert "1080" not in caption


def test_a_video_with_no_reported_height_names_the_requested_tier(tmp_path: Path) -> None:
    assert "🎞 720p" in _upload_caption(
        _result(tmp_path, quality="720"), EN, source_url=URL
    )
    assert f"🎞 {t('media.quality_max', EN)}" in _upload_caption(
        _result(tmp_path, quality="best"), EN, source_url=URL
    )


def test_the_bot_line_carries_the_handle(tmp_path: Path) -> None:
    assert f"🤖 @{BOT}" in _upload_caption(_result(tmp_path), EN)


def test_a_bot_without_a_booted_handle_omits_the_line(tmp_path: Path) -> None:
    delivery.set_bot_username("")

    caption = _upload_caption(_result(tmp_path), EN)

    assert "🤖" not in caption
    assert caption.splitlines()[0] == "🎬 A Clip"


# ---------------------------------------------------------------------------
# Missing and hostile metadata
# ---------------------------------------------------------------------------


def test_a_missing_title_omits_its_line(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, title=""), EN, source_url=URL)

    assert "🎬" not in caption
    assert caption.splitlines()[0] == f"🔗 {URL}"


def test_a_title_that_is_the_url_is_not_said_twice(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, title=URL), EN, source_url=URL)

    assert "🎬" not in caption
    assert caption.count(URL) == 1


def test_no_link_means_no_link_line(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path), EN)

    assert "🔗" not in caption
    assert caption.splitlines() == [
        "🎬 A Clip",
        "🎞 " + t("media.quality_max", EN) + " • 2.0 KB",
        f"🤖 @{BOT}",
    ]


def test_the_caption_escapes_html_in_titles_and_links(tmp_path: Path) -> None:
    """Both fields are attacker-controlled text in an HTML-parsed message."""
    caption = _upload_caption(
        _result(tmp_path, title="<script>alert(1)</script>"),
        EN,
        source_url="https://evil.example/<script>alert(1)</script>",
    )

    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption


# ---------------------------------------------------------------------------
# Audio: the container and its level
# ---------------------------------------------------------------------------


def test_audio_captions_name_the_container_and_its_real_rate(tmp_path: Path) -> None:
    assert "🎧 MP3 · 256 kbps" in _upload_caption(
        _result(tmp_path, media_format="audio", quality="mp3.high"), EN, source_url=URL
    )
    # Two canonical spellings predate the presets and mean their own thing: `mp3`
    # is the balanced 192k re-encode, `m4a` the untouched source stream.
    assert "🎧 MP3 · 192 kbps" in _upload_caption(
        _result(tmp_path, media_format="audio", quality="mp3"), EN
    )
    assert "🎧 M4A · Original" in _upload_caption(
        _result(tmp_path, media_format="audio", quality="m4a"), EN
    )
    # PCM and FLAC have no knob: the container is the whole label.
    assert "🎧 WAV · 2.0 KB" in _upload_caption(
        _result(tmp_path, media_format="audio", quality="wav"), EN
    )
    assert "🎧 FLAC · 2.0 KB" in _upload_caption(
        _result(tmp_path, media_format="audio", quality="flac"), EN
    )


def test_the_copied_stream_is_named_in_the_readers_language(tmp_path: Path) -> None:
    persian = _upload_caption(
        _result(tmp_path, media_format="audio", quality="m4a"), FA
    )

    assert f"🎧 M4A · {t('media.original', FA)}" in persian


def test_a_song_is_captioned_as_the_song(tmp_path: Path) -> None:
    """Title, credits, length and what this file is — under the link the *user*
    sent, never the mapped YouTube video the song was fetched from."""
    track = spotify_module.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Believer",
        artists=("Artist",),
        duration_s=200,
    )
    spotify_url = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"

    caption = _upload_caption(
        _result(tmp_path, media_format="audio", quality="mp3"),
        EN,
        track,
        source_url=spotify_url,
    )

    assert caption.splitlines() == [
        "🎵 Believer",
        "🎤 Artist",
        "⏱ 3:20",
        "🎧 MP3 · 192 kbps · 2.0 KB",
        f"🔗 {spotify_url}",
        f"🤖 @{BOT}",
    ]
    assert "youtube" not in caption.lower()


def test_the_album_line_carries_the_release(tmp_path: Path) -> None:
    track = spotify_module.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="I'm The Man",
        artists=("ADÉLA",),
        duration_s=150,
        album="PRIMA",
        year=2026,
    )

    caption = _upload_caption(
        _result(tmp_path, media_format="audio", quality="mp3.best"), EN, track
    )

    assert "🎵 I'm The Man" in caption
    assert "💿 PRIMA · 2026" in caption


def test_a_length_is_shown_the_way_a_player_shows_it() -> None:
    assert delivery.format_duration(150) == "2:30"
    assert delivery.format_duration(3725) == "1:02:05"
    assert delivery.format_duration(None) == ""
    assert delivery.format_duration(0) == ""


# ---------------------------------------------------------------------------
# The replay: a cached file gets the same card
# ---------------------------------------------------------------------------


def test_a_replayed_file_gets_the_same_card() -> None:
    row = cast(
        asyncpg.Record,
        {"original_url": URL, "quality": "video:720"},
    )

    caption = delivery.replay_caption(row, EN)

    assert caption.splitlines() == [
        f"🔗 {URL}",
        "🎞 720p",
        f"🤖 @{BOT}",
    ]


def test_a_replay_names_the_request_by_its_rules() -> None:
    def label(quality: str) -> str:
        row = cast(asyncpg.Record, {"original_url": URL, "quality": quality})
        return delivery.replay_caption(row, EN)

    assert "🎞 " + t("media.quality_max", EN) in label("video")  # the default tier
    assert "🎧 MP3 · 256 kbps" in label("audio:mp3.high")  # a deliberate tier
    assert "🎧 WAV" in label("audio:wav")
    assert "🎧 FLAC" in label("audio:flac")


def test_a_row_that_knows_nothing_shows_nothing_empty() -> None:
    """An older row, a stub — no empty 🔗, no invented quality."""
    caption = delivery.replay_caption(cast(asyncpg.Record, {}), FA)

    assert "🔗" not in caption
    assert caption  # ...but the bot line (at boot) still identifies the file
    assert f"🤖 @{BOT}" in caption


def test_label_for_request_refuses_junk() -> None:
    assert delivery.label_for_request("", EN) == ""
    assert delivery.label_for_request("best", EN) == ""
