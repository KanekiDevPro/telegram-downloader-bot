"""Spotify is audio, genuinely: MP3-320 HQ and the untouched original, tagged as the song.

The mapped YouTube stand-in could produce anything, but the song the user asked
for is an audio file — never a video rung, never a dummy row, and never a fake
FLAC (a lossless container around lossy audio is a bigger file, not better
sound). And the file that arrives must *be* the song: Spotify's own title,
artists, album and cover are written into the audio (ID3/atoms/Vorbis via
ffmpeg, no re-encode) and ride the ``send_audio`` call, instead of a
YouTube-fallback filename with a stranger's thumbnail.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from handlers import user as user_module
from services import content, spotify
from services import worker as worker_module
from services.extractor import AudioCapability, DownloadResult, MediaInfo

SPOTIFY_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
SOUNDCLOUD_URL = "https://soundcloud.com/a/b"


def _track() -> spotify.SpotifyTrack:
    return spotify.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Never Gonna Give You Up",
        artists=("Rick Astley",),
        duration_s=213,
        album="Whenever You Need Somebody",
        year=1987,
        covers=(
            (64, "https://i.scdn.co/image/abc64"),
            (300, "https://i.scdn.co/image/abc300"),
        ),
    )


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


# ---------------------------------------------------------------------------
# Only MP3 and FLAC are offered
# ---------------------------------------------------------------------------


def test_a_spotify_track_is_offered_clean_audio_formats_only() -> None:
    assert content.routing_for(SPOTIFY_URL).audio_formats == ("mp3",)


def test_the_spotify_question_has_no_video_or_dummy_rows() -> None:
    capability = AudioCapability(
        formats=("mp3", "m4a", "flac", "opus", "wav"), copy_ok=False
    )

    keyboard = user_module._question_keyboard(SPOTIFY_URL, "en", capability=capability)
    callbacks = [data for _label, data in _buttons(keyboard)]

    assert "fmt:audio:mp3.best" in callbacks, "the HQ transcode is its own row"
    assert "fmt:audio:m4a" in callbacks, "the untouched original is its own row"
    assert "fmt:audio:flac" not in callbacks, "no fake FLAC"
    assert not [data for data in callbacks if data.startswith("audf:")], "no preset grid"
    assert not [data for data in callbacks if data.startswith("fmt:video:")], "no video rungs"
    assert "fmt:audio:wav" not in callbacks


def test_an_ordinary_audio_link_keeps_its_full_grid() -> None:
    # "Full" now means every honestly fillable format: lossless rows are
    # hidden until a provider can honestly produce them
    # (content.lossless_offered) — the grid below is the whole menu.
    assert content.routing_for(SOUNDCLOUD_URL).audio_formats == (
        "mp3",
        "m4a",
        "opus",
    )


def test_a_crafted_non_spotify_codec_is_refused_on_a_track() -> None:
    data = {"audio_offered": ["mp3"], "copy_ok": False}

    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "opus.balanced", data) is False
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "m4a.balanced", data) is False
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "flac", data) is False
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "mp3.best", data) is True
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "m4a", data) is True
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "best", data) is False


# ---------------------------------------------------------------------------
# The file is tagged as the song (ffmpeg, no re-encode)
# ---------------------------------------------------------------------------


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    assert exe is not None, "ffmpeg is the tagging engine"
    return exe


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    assert exe is not None, "ffprobe reads the tags back"
    return exe


def _make_mp3(path: Path) -> None:
    subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _make_cover(path: Path) -> None:
    subprocess.run(
        [
            _ffmpeg(),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=64x64:d=1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
        timeout=120,
    )


def _read_tags(path: Path) -> str:
    done = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format_tags=title,artist,album,date",
            "-of",
            "default=nw=1",
            str(path),
        ],
        check=True,
        timeout=120,
        capture_output=True,
        text=True,
    )
    return done.stdout


def _has_attached_pic(path: Path) -> bool:
    done = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type:stream_disposition=attached_pic",
            "-of",
            "csv",
            str(path),
        ],
        check=True,
        timeout=120,
        capture_output=True,
        text=True,
    )
    # csv prints values without keys: ``stream,video,1`` is the attached picture.
    return "stream,video,1" in done.stdout.replace("\r", "")


async def test_the_track_is_written_into_the_file_itself(tmp_path: Path) -> None:
    """End to end through the real ffmpeg: tags *and* the cover land in the bytes."""
    src = tmp_path / "rip.mp3"
    _make_mp3(src)
    cover = tmp_path / "cover.jpg"
    _make_cover(cover)

    assert spotify.tag_audio(src, _track(), cover) is True

    tags = _read_tags(src)
    assert "title=Never Gonna Give You Up" in tags
    assert "artist=Rick Astley" in tags
    assert "album=Whenever You Need Somebody" in tags
    assert "1987" in tags
    assert _has_attached_pic(src), "the cover rides in the file, not beside it"


async def test_tagging_without_a_cover_still_names_the_song(tmp_path: Path) -> None:
    src = tmp_path / "rip.mp3"
    _make_mp3(src)

    assert spotify.tag_audio(src, _track(), None) is True
    assert "title=Never Gonna Give You Up" in _read_tags(src)


async def test_a_failed_tag_leaves_the_file_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "rip.mp3"
    _make_mp3(src)
    before = src.read_bytes()

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(spotify.subprocess, "run", explode)

    assert spotify.tag_audio(src, _track(), None) is False
    assert src.read_bytes() == before


async def test_tagging_without_ffmpeg_is_a_clean_no(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "rip.mp3"
    src.write_bytes(b"not really an mp3")
    monkeypatch.setattr(spotify.shutil, "which", lambda _name: None)

    assert spotify.tag_audio(src, _track(), None) is False


async def test_a_video_file_is_never_tagged_as_a_song(tmp_path: Path) -> None:
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"not really a video")

    assert spotify.tag_audio(src, _track(), None) is False


def test_the_song_is_filed_under_its_own_name() -> None:
    assert spotify.track_filename(_track(), ".mp3") == "Rick Astley — Never Gonna Give You Up.mp3"


def test_the_filename_survives_hostile_characters() -> None:
    track = spotify.SpotifyTrack(
        track_id="x", title='a/b:c*d?e"f<g>h|i', artists=("R*ck",), duration_s=1
    )

    name = spotify.track_filename(track, ".mp3")

    assert "/" not in name and "\\" not in name
    assert name.endswith(".mp3")


# ---------------------------------------------------------------------------
# The upload carries the song, and the file is prepared before it
# ---------------------------------------------------------------------------


class _AudioBot:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(audio=SimpleNamespace(file_id="audio-1"))


def _media_info() -> MediaInfo:
    return MediaInfo(
        source_url="https://youtu.be/abc",
        title="Rick Astley - Never Gonna Give You Up (Official Video)",
        platform="youtube",
        webpage_url="https://youtu.be/abc",
        extension="mp3",
        thumbnail=None,
        duration=213,
        filesize_approx=1024,
        is_live=False,
    )


async def test_the_audio_send_is_identified_as_the_song(tmp_path: Path) -> None:
    src = tmp_path / "rip.mp3"
    _make_mp3(src)
    cover = tmp_path / "cover.jpg"
    _make_cover(cover)
    bot = _AudioBot()

    file_id = await worker_module._send_file(
        cast(Any, bot),
        1,
        src,
        "audio",
        "caption",
        track=_track(),
        cover=cover,
    )

    assert file_id == "audio-1"
    sent = bot.calls[0]
    assert sent["performer"] == "Rick Astley"
    assert sent["title"] == "Never Gonna Give You Up"
    assert sent["thumbnail"] is not None, "the cover rides the send too"


async def test_the_track_is_renamed_and_tagged_before_upload(tmp_path: Path) -> None:
    src = tmp_path / "youtube-rip.mp3"
    src.write_bytes(b"bytes")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"cover")
    result = DownloadResult(
        file_path=src, info=_media_info(), media_format="audio", quality="mp3"
    )
    seen: dict[str, Any] = {}

    def fake_tag(path: Path, track: spotify.SpotifyTrack, art: Any = None) -> bool:
        seen["path"] = path
        seen["track"] = track
        seen["cover"] = art
        return True

    monkeypatch_tag = fake_tag
    import unittest.mock as mock

    with mock.patch.object(spotify, "tag_audio", monkeypatch_tag):
        prepared = await worker_module._prepare_track_file(result, _track(), cover)

    assert prepared.file_path.name == "Rick Astley — Never Gonna Give You Up.mp3"
    assert prepared.file_path.exists()
    assert seen["path"] == prepared.file_path
    assert seen["track"].track_id == _track().track_id
    assert seen["cover"] == cover


async def test_a_tagging_failure_still_renames_and_uploads(tmp_path: Path) -> None:
    src = tmp_path / "youtube-rip.mp3"
    src.write_bytes(b"bytes")
    result = DownloadResult(
        file_path=src, info=_media_info(), media_format="audio", quality="mp3"
    )
    import unittest.mock as mock

    with mock.patch.object(spotify, "tag_audio", lambda *args, **kwargs: False):
        prepared = await worker_module._prepare_track_file(result, _track(), None)

    assert prepared.file_path.name == "Rick Astley — Never Gonna Give You Up.mp3"
    assert prepared.file_path.exists(), "the file is never lost to a tag failure"
