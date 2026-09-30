"""Regression tests: progress display, YouTube discovery, Spotify isolation.

Production showed three failures: audio downloads with no useful progress,
YouTube menus with a single 360p rung, and Spotify menus exposing YouTube
video ladders. Each test below pins the production code path (fakes only at
the network edges), so a bypass of the fixed code fails the suite.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from services import cache as cache_module
from services.extractor import MediaInfo, VideoOption

YOUTUBE = "https://www.youtube.com/watch?v=abc123xyz01"
SPOTIFY = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
MAPPED = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# Shared fakes (network edges only)
# ---------------------------------------------------------------------------


def _stream(
    format_id: str,
    height: int | None,
    width: int = 0,
    *,
    vcodec: str | None = "avc1.64001f",
    acodec: str | None = "mp4a.40.2",
    ext: str = "mp4",
    filesize: int = 1_000_000,
) -> dict[str, Any]:
    return {
        "format_id": format_id,
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
        "height": height,
        "width": width,
        "filesize": filesize,
    }


def _ladder_info() -> dict[str, Any]:
    """360/480/720 muxed plus video-only 1080 with its own audio stream."""
    return {
        "title": "A Clip",
        "extractor_key": "Youtube",
        "webpage_url": YOUTUBE,
        "ext": "mp4",
        "duration": 120,
        "formats": [
            _stream("18", 360, 640),
            _stream("22", 720, 1280, filesize=4_000_000),
            _stream("399", 480, 854, filesize=2_000_000),
            _stream("137", 1080, 1920, vcodec="avc1.640028", acodec="none",
                      filesize=8_000_000),
            _stream("140", None, vcodec="none", acodec="mp4a.40.2", ext="m4a",
                      filesize=500_000),
        ],
    }


def _narrow_info() -> dict[str, Any]:
    """What a restricted client sees: a single 360p stream."""
    return {
        "title": "A Clip",
        "extractor_key": "Youtube",
        "webpage_url": YOUTUBE,
        "ext": "mp4",
        "duration": 120,
        "formats": [_stream("18", 360, 640)],
    }


def _fresh_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=7, user_id=7)
    )


class _FakeMessage:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []
        self.chat = SimpleNamespace(id=7)

    async def answer(self, text: str, **kwargs: Any) -> None:
        self.texts.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def answer_photo(self, **kwargs: Any) -> None:
        self.markups.append(kwargs.get("reply_markup"))
        self.texts.append(kwargs.get("caption", ""))


class _FakeBot:
    async def send_chat_action(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return True


def _video_buttons(markup: Any) -> list[str]:
    out: list[str] = []
    for row in markup.inline_keyboard:
        for button in row:
            data = button.callback_data or ""
            if data.startswith("fmt:video:"):
                out.append(data)
    return out


def _all_callbacks(markup: Any) -> list[str]:
    return [
        button.callback_data or ""
        for row in markup.inline_keyboard
        for button in row
    ]


# ---------------------------------------------------------------------------
# Group 5 — progress rendering
# ---------------------------------------------------------------------------


def test_progress_renders_bytes_speed_eta_and_elapsed() -> None:
    from services import worker as worker_module

    text = worker_module._render_download_progress(
        {
            "status": "downloading",
            "downloaded_bytes": 12_400_000,
            "total_bytes": 24_100_000,
            "speed": 3_800_000,
            "eta": 4,
        },
        elapsed_s=9.0,
        lang="en",
    )

    assert "52%" in text or "51%" in text
    assert "MB" in text
    assert "ETA" in text
    assert "Elapsed" in text


def test_progress_with_unknown_total_never_fakes_a_percent() -> None:
    from services import worker as worker_module

    text = worker_module._render_download_progress(
        {"status": "downloading", "downloaded_bytes": 5_000_000, "speed": 1_000_000},
        elapsed_s=5.0,
        lang="en",
    )

    assert "%" not in text
    assert "nan" not in text.lower()


def test_progress_finished_state_is_distinguishable() -> None:
    from services import worker as worker_module

    text = worker_module._render_download_progress(
        {"status": "finished"},
        elapsed_s=12.0,
        lang="en",
    )
    downloading = worker_module._render_download_progress(
        {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2},
        elapsed_s=1.0,
        lang="en",
    )

    assert text != downloading


async def test_progress_hook_throttles_rapid_updates() -> None:
    import asyncio
    import time as _time

    from services import worker as worker_module

    scheduled: list[str] = []
    editor = worker_module._ProgressEditor(SimpleNamespace(), "en", "card")

    async def record(text: str) -> None:
        scheduled.append(text)

    editor._edit = record  # type: ignore[method-assign]
    editor._last_edit = _time.monotonic()
    editor.hook(
        {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100}
    )
    await asyncio.sleep(0.05)

    assert scheduled == []


async def test_progress_hook_never_raises_on_garbage() -> None:
    from services import worker as worker_module

    editor = worker_module._ProgressEditor(SimpleNamespace(), "en", "card")
    editor.hook({})
    editor.hook({"status": "downloading"})
    editor.hook({"status": "finished"})
    editor.post_hook({})
    editor.post_hook({"status": "started", "postprocessor": "FFmpegExtractAudio"})


# ---------------------------------------------------------------------------
# Group 1 — format discovery merge
# ---------------------------------------------------------------------------


def test_merged_probe_view_exposes_every_available_rung() -> None:
    from services import extractor as extractor_module

    narrow = extractor_module._to_media_info(YOUTUBE, _narrow_info())
    wide = extractor_module._to_media_info(YOUTUBE, _ladder_info())

    merged = extractor_module.merge_format_infos(narrow, wide)

    labels = {option.label_p for option in merged.video_options}
    assert {360, 480, 720, 1080} <= labels


def test_merge_dedupes_and_lets_the_winner_name_the_media() -> None:
    from services import extractor as extractor_module

    narrow = extractor_module._to_media_info(YOUTUBE, _narrow_info())
    wide = extractor_module._to_media_info(YOUTUBE, _ladder_info())

    merged = extractor_module.merge_format_infos(narrow, wide)

    assert merged.title == narrow.title
    heights = [option.height for option in merged.video_options]
    assert len(heights) == len(set(heights))


async def test_probe_merges_the_straggler_instead_of_dropping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First answer wins the metadata, but a slower leg's formats must still
    reach the menu — otherwise the menu is the narrow client's menu."""
    from services import extractor as extractor_module
    from services.extractor import ExtractorService

    narrow = extractor_module._to_media_info(YOUTUBE, _narrow_info())
    wide = extractor_module._to_media_info(YOUTUBE, _ladder_info())

    def fake_extract_sync(
        self: ExtractorService, url: str, *, youtube_clients: Any = None
    ) -> MediaInfo:
        import time as _time

        _time.sleep(0.3 if (youtube_clients or ("narrow",))[0] == "wide" else 0)
        return wide if (youtube_clients or ("narrow",))[0] == "wide" else narrow

    monkeypatch.setattr(ExtractorService, "_extract_sync", fake_extract_sync)
    monkeypatch.setattr(
        extractor_module, "METADATA_CLIENT_PRIORITY", ["narrow", "wide"]
    )

    service = ExtractorService(
        download_dir=cast(Any, __import__("pathlib").Path(".")),
        youtube_clients=("narrow", "wide"),
    )
    result = await service._probe(YOUTUBE)

    assert {360, 480, 720, 1080} <= {o.label_p for o in result.video_options}


# ---------------------------------------------------------------------------
# Group 3 — cache isolation across sources
# ---------------------------------------------------------------------------


def test_youtube_video_and_spotify_audio_keys_never_collide() -> None:
    video = cache_module.cache_key(YOUTUBE, "video", "1080")
    audio = cache_module.cache_key(SPOTIFY, "audio", "m4a")

    assert video != audio


def test_mapped_url_does_not_merge_cache_identities() -> None:
    """Spotify may resolve to this YouTube URL internally — the stored rows
    still belong to the link the user sent, never to the stand-in."""
    spotify_row = cache_module.cache_key(SPOTIFY, "audio", "m4a")
    youtube_row = cache_module.cache_key(MAPPED, "video", "1080")

    assert spotify_row != youtube_row


def test_audio_uploads_store_no_video_ladder() -> None:
    from services import worker as worker_module
    from services.queue import DownloadTask

    task = DownloadTask(
        url=SPOTIFY, telegram_id=1, chat_id=1, media_format="audio", quality="m4a"
    )
    info = SimpleNamespace(video_options=(VideoOption(1080, 1, False, 1920),))

    assert worker_module._upload_ladder(task, info) == ()


def test_video_uploads_keep_their_ladder() -> None:
    from services import worker as worker_module
    from services.queue import DownloadTask

    task = DownloadTask(
        url=YOUTUBE, telegram_id=1, chat_id=1, media_format="video", quality="1080"
    )
    ladder = (VideoOption(1080, 1, False, 1920),)
    info = SimpleNamespace(video_options=ladder)

    assert worker_module._upload_ladder(task, info) == ladder


# ---------------------------------------------------------------------------
# Groups 2 + 4 — Spotify isolation and FSM replacement
# ---------------------------------------------------------------------------


def _video_row(url: str, ladder: str) -> dict[str, Any]:
    return {
        "quality": "video:1080",
        "title": "A Clip",
        "label": "1080p",
        "ladder": ladder,
        "telegram_file_id": "file-1",
        "kind": "video",
    }


def _audio_row() -> dict[str, Any]:
    return {
        "quality": "audio:m4a",
        "title": "Never Gonna Give You Up",
        "label": "Original",
        "ladder": "",
        "telegram_file_id": "file-2",
        "kind": "audio",
    }


async def test_spotify_menu_from_contaminated_cache_shows_no_video() -> None:
    """Stale video rows (and a YouTube ladder) stored under a Spotify URL —
    the bug as produced — must never become its menu. With only video rows
    there is no audio menu to draw, so the question falls through to the
    probe instead of showing 1080p; mixed rows still replay their audio."""
    from handlers import user as user_module
    from services.extractor import VideoOption as _VO

    ladder = cache_module.serialize_ladder([_VO(1080, 8_000_000, True, 1920)])
    message = _FakeMessage()
    state = _fresh_state()
    await state.update_data(
        url=YOUTUBE, offered=["1080"], options=[(1080, 1080, 1, False)]
    )

    asked =    await user_module._ask_from_cache(
        cast(Any, message), state, SPOTIFY, "en", [_video_row(SPOTIFY, ladder)], edit=False
    )

    assert asked is False
    assert message.markups == []

    message2 = _FakeMessage()
    state2 = _fresh_state()
    asked2 = await user_module._ask_from_cache(
        cast(Any, message2), state2, SPOTIFY, "en",
        [_video_row(SPOTIFY, ladder), _audio_row()], edit=False,
    )

    assert asked2 is True
    assert _video_buttons(message2.markups[-1]) == []
    assert "fmt:audio:m4a" in _all_callbacks(message2.markups[-1])
    data = await state2.get_data()
    assert data["url"] == SPOTIFY
    assert all(
        not str(tier).isdigit() for tier in (data.get("offered") or [])
    )


async def test_spotify_audio_rows_still_replay() -> None:
    from handlers import user as user_module

    message = _FakeMessage()
    state = _fresh_state()

    await user_module._ask_from_cache(
        cast(Any, message), state, SPOTIFY, "en", [_audio_row()], edit=False
    )

    callbacks = _all_callbacks(message.markups[-1])
    assert "fmt:audio:m4a" in callbacks
    assert _video_buttons(message.markups[-1]) == []


async def test_youtube_then_spotify_replaces_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full sequence: a YouTube menu, then a Spotify link on the same
    session state — no video ladder may survive the switch."""
    from handlers import user as user_module
    from services import extractor as extractor_module
    from services import spotify as spotify_module

    async def fake_probe_meta(bot: Any, url: str) -> MediaInfo:
        return extractor_module._to_media_info(url, _ladder_info())

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return SimpleNamespace(
            track_id="4uLU6hMCjMI75M1A2tKUQC",
            title="Never Gonna Give You Up",
            artists=("Rick Astley",),
            duration_s=213,
            album="",
            year=None,
            isrc=None,
        )

    monkeypatch.setattr(user_module, "_probe_meta", fake_probe_meta)
    monkeypatch.setattr(spotify_module, "lookup", fake_lookup)

    message = _FakeMessage()
    state = _fresh_state()
    bot = _FakeBot()
    user: Any = {"telegram_id": 7, "language": "en"}

    await user_module._ask_about_link(
        cast(Any, message), state, user, YOUTUBE, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    first = await state.get_data()
    assert first["url"] == YOUTUBE
    assert any(str(tier).isdigit() for tier in (first.get("offered") or []))

    message2 = _FakeMessage()
    await user_module._ask_about_link(
        cast(Any, message2), state, user, SPOTIFY, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    second = await state.get_data()
    assert second["url"] == SPOTIFY
    assert (second.get("options") or []) == []
    assert not any(str(tier).isdigit() for tier in (second.get("offered") or []))
    assert _video_buttons(message2.markups[-1]) == []


async def test_youtube_spotify_youtube_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YouTube → Spotify → YouTube: each question belongs to its own link —
    the video ladder never leaks into the track, and the track never trims
    the video menu that follows it."""
    from handlers import user as user_module
    from services import extractor as extractor_module
    from services import spotify as spotify_module

    async def fake_probe_meta(bot: Any, url: str) -> MediaInfo:
        return extractor_module._to_media_info(url, _ladder_info())

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return SimpleNamespace(
            track_id="4uLU6hMCjMI75M1A2tKUQC",
            title="Never Gonna Give You Up",
            artists=("Rick Astley",),
            duration_s=213,
            album="",
            year=None,
            isrc=None,
        )

    monkeypatch.setattr(user_module, "_probe_meta", fake_probe_meta)
    monkeypatch.setattr(spotify_module, "lookup", fake_lookup)

    state = _fresh_state()
    bot = _FakeBot()
    user: Any = {"telegram_id": 7, "language": "en"}

    first = _FakeMessage()
    await user_module._ask_about_link(
        cast(Any, first), state, user, YOUTUBE, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    assert "fmt:video:1080" in _all_callbacks(first.markups[-1])

    second = _FakeMessage()
    await user_module._ask_about_link(
        cast(Any, second), state, user, SPOTIFY, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    assert _video_buttons(second.markups[-1]) == []
    assert "fmt:audio:m4a" in _all_callbacks(second.markups[-1])

    third = _FakeMessage()
    await user_module._ask_about_link(
        cast(Any, third), state, user, YOUTUBE, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    data = await state.get_data()
    assert data["url"] == YOUTUBE
    assert "fmt:video:1080" in _all_callbacks(third.markups[-1])


async def test_spotify_then_youtube_restores_video_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from handlers import user as user_module
    from services import extractor as extractor_module
    from services import spotify as spotify_module

    async def fake_probe_meta(bot: Any, url: str) -> MediaInfo:
        return extractor_module._to_media_info(url, _ladder_info())

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        return SimpleNamespace(
            track_id="4uLU6hMCjMI75M1A2tKUQC",
            title="Never Gonna Give You Up",
            artists=("Rick Astley",),
            duration_s=213,
            album="",
            year=None,
            isrc=None,
        )

    monkeypatch.setattr(user_module, "_probe_meta", fake_probe_meta)
    monkeypatch.setattr(spotify_module, "lookup", fake_lookup)

    message = _FakeMessage()
    state = _fresh_state()
    bot = _FakeBot()
    user: Any = {"telegram_id": 7, "language": "en"}

    await user_module._ask_about_link(
        cast(Any, message), state, user, SPOTIFY, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    assert (await state.get_data())["url"] == SPOTIFY

    message2 = _FakeMessage()
    await user_module._ask_about_link(
        cast(Any, message2), state, user, YOUTUBE, "en",
        bot=cast(Any, bot), pool=cast(Any, object()), queue=cast(Any, None),
    )
    data = await state.get_data()
    assert data["url"] == YOUTUBE
    assert "fmt:video:1080" in _all_callbacks(message2.markups[-1])
