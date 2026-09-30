"""Spotify's honest tiers: MP3-320 HQ and the untouched original — never fake FLAC.

A mapped YouTube stand-in serves ~160 kbps lossy audio at best. Transcoding
that into a FLAC container claims lossless sound the source never had (a
bigger file, not better sound), so FLAC leaves the menu. What stays is honest:
``MP3 (320 kbps)`` — the HQ transcode, tagged ID3v2.3 — and ``Original Audio
(Best)`` — the site's own Opus/M4A/AAC stream copied without re-encoding.
Whatever arrives still carries the song: title, artists, album and cover ride
the bytes (ffmpeg, no re-encode) and the ``send_audio`` call alike.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.i18n import t
from handlers import user as user_module
from services import content, spotify
from services.extractor import AudioCapability

SPOTIFY_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
USER_ID = 4242
EN = "en"


def _track() -> spotify.SpotifyTrack:
    return spotify.SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Never Gonna Give You Up",
        artists=("Rick Astley",),
        duration_s=213,
        album="Whenever You Need Somebody",
        year=1987,
        covers=((300, "https://i.scdn.co/image/abc300"),),
    )


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


# ---------------------------------------------------------------------------
# The menu: two honest rows, no FLAC, no preset grid
# ---------------------------------------------------------------------------


def test_no_fake_flac_in_the_spotify_routing() -> None:
    """FLAC claims lossless sound a lossy stand-in never had — it is not offered."""
    assert "flac" not in content.routing_for(SPOTIFY_URL).audio_formats


def test_the_spotify_question_is_two_honest_rows() -> None:
    """MP3-320 HQ and the untouched original — direct taps, no video, no grid."""
    capability = AudioCapability(
        formats=("mp3", "m4a", "flac", "opus", "wav"), copy_ok=False
    )

    rows = _buttons(user_module._question_keyboard(SPOTIFY_URL, EN, capability=capability))
    datas = [data for _, data in rows]

    assert "fmt:audio:mp3.best" in datas, f"the HQ transcode row is missing: {rows!r}"
    assert "fmt:audio:m4a" in datas, f"the original-audio row is missing: {rows!r}"
    assert "fmt:audio:flac" not in datas, "fake FLAC must not be offered"
    assert not [data for data in datas if data.startswith("audf:")], (
        f"no preset grid on a track — the tiers are the two rows: {rows!r}"
    )
    assert not [data for data in datas if data.startswith("fmt:video:")], "no video rungs"


def test_the_two_rows_wear_their_honest_labels() -> None:
    capability = AudioCapability(formats=("mp3",), copy_ok=False)

    names = dict(_buttons(user_module._question_keyboard(SPOTIFY_URL, EN, capability=capability)))

    assert names.get(t("fmt.spotify_mp3", EN)) == "fmt:audio:mp3.best"
    assert names.get(t("fmt.spotify_original", EN)) == "fmt:audio:m4a"


def test_spotify_taps_are_judged_by_the_two_rows() -> None:
    """The HQ tap, the original tap — and nothing else — validate."""
    data = {"offered": [], "audio_offered": ["mp3"], "options": [], "copy_ok": True}

    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "mp3.best", data) is True
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "m4a", data) is True
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "flac", data) is False
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "opus.balanced", data) is False
    assert user_module._tap_was_offered(SPOTIFY_URL, "audio", "best", data) is False


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return _message(method.text or "", self)
        return True

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    @property
    def edits(self) -> list[EditMessageText]:
        return [call for call in self.calls if isinstance(call, EditMessageText)]


def _message(text: str, bot: RecordingBot) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=_message("menu", bot),
    ).as_(cast(Bot, bot))


async def test_a_spotify_preset_grid_tap_is_refused() -> None:
    """No preset grid is drawn on a track, so ``audf:`` taps are answered, not run."""
    bot = RecordingBot()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    await state.set_state(user_module.DownloadStates.waiting_format)
    await state.update_data(url=SPOTIFY_URL, title="Never Gonna Give You Up")

    await user_module.on_audio_format(_callback(bot, "audf:mp3"), state, lang=EN)

    assert bot.answers, "a refused tap is still answered (no spinner left behind)"
    assert bot.answers[-1].text == t("intake.no_format", EN)
    assert bot.answers[-1].show_alert is True
    assert bot.edits == [], "no menu opens for a grid that was never drawn"


# ---------------------------------------------------------------------------
# The bytes: an m4a original carries the song too (ffmpeg, no re-encode)
# ---------------------------------------------------------------------------


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    assert exe is not None, "ffmpeg is the tagging engine"
    return exe


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    assert exe is not None, "ffprobe reads the tags back"
    return exe


def _make_m4a(path: Path) -> None:
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
            "aac",
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
    return "stream,video,1" in done.stdout.replace("\r", "")


async def test_tagging_an_m4a_original_keeps_song_and_cover(tmp_path: Path) -> None:
    """The untouched stream is still the song: tags and cover land in the bytes."""
    src = tmp_path / "rip.m4a"
    _make_m4a(src)
    cover = tmp_path / "cover.jpg"
    _make_cover(cover)

    assert spotify.tag_audio(src, _track(), cover) is True

    tags = _read_tags(src)
    assert "title=Never Gonna Give You Up" in tags
    assert "artist=Rick Astley" in tags
    assert "album=Whenever You Need Somebody" in tags
    assert "1987" in tags
    assert _has_attached_pic(src), "the cover rides in the file, not beside it"
