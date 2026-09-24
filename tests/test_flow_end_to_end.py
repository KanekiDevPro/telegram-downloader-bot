"""The whole request flow, fixture-driven and offline.

URL → probe → capability → menu → callback → download result → verification →
final caption, exercised as one chain for every shape the release cares about:
a YouTube ladder, an undiscovered ladder, a resolvable and an unresolvable
Spotify link, a stale tap, a selection that vanished before the download, and
native vs converted audio. Everything between the Telegram update and the
ffprobe call is the real code path (handlers, FSM offer lists, worker
verification, caption builder); the edges are fixtures — a recording bot, a
canned extractor, an in-memory queue, a canned probe. No network, no Postgres,
no Redis — so this belongs in the normal unit suite.

The assertions follow the honesty contracts: only real rungs are offered, a
tap that was not offered queues nothing, a delivered rung that differs from
the selected one fails before anything is sent, and every caption names what
the file *is* (native «Original» vs converted target rate, with the source's
own rate named when the target exceeds it).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.config import Settings
from core.i18n import t
from handlers import user as user_module
from services import subscription as subscription_module
from services import verify, worker
from services.extractor import (
    AudioCapability,
    DownloadResult,
    ExtractionError,
    MediaInfo,
    VideoOption,
)
from services.verify import MediaFacts

USER_ID = 4242
EN = "en"
YOUTUBE_URL = "https://youtu.be/abc"
SOUNDCLOUD_URL = "https://soundcloud.com/a/b"
SPOTIFY_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


# ---------------------------------------------------------------------------
# Harness: a recording bot, an in-memory queue, canned extractor/probe
# ---------------------------------------------------------------------------


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.state: Any = SimpleNamespace()

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return _message(method.text or "", self)
        return True

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def edits(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, EditMessageText)]

    @property
    def screens(self) -> list[str]:
        return [
            call.text or ""
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
        ]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    @property
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText)) and call.reply_markup is not None
        ]


def _message(text: str, bot: RecordingBot, user_id: int = USER_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="user"),
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


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _user() -> Any:
    return {
        "telegram_id": USER_ID,
        "username": "ali",
        "is_premium": False,
        "premium_until": None,
        "language": EN,
        "is_new": False,
    }


def _fake_queue() -> Any:
    return FakeQueue()


def _fresh_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


class FakeQueue:
    """The queue stand-in — handed over loosely typed (handlers declare
    ``TaskQueue``), the same way test_user_menu.py does it."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return len(self.tasks)

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


class _NoRefusal:
    """``preflight`` with nothing to say (the real rules have their own tests)."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(refused=False, message="")

    @staticmethod
    def clear_anonymous_refusal() -> None:
        return None


class _FakeExtractor:
    def __init__(self, info: MediaInfo) -> None:
        self.info = info
        self.urls: list[str] = []

    async def extract(self, url: str) -> MediaInfo:
        self.urls.append(url)
        return self.info


def _bot_with(info: MediaInfo | None) -> RecordingBot:
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=_FakeExtractor(info)) if info else SimpleNamespace()
    return bot


def _info(**over: Any) -> MediaInfo:
    base: dict[str, Any] = {
        "source_url": YOUTUBE_URL,
        "title": "A Clip",
        "platform": "youtube",
        "webpage_url": YOUTUBE_URL,
        "extension": "mp4",
        "thumbnail": None,
        "duration": 95,
        "filesize_approx": 1,
        "is_live": False,
        "video_options": (
            VideoOption(360, 900 * 1024, True),
            VideoOption(720, 8 * 1024 * 1024, True),
            VideoOption(1080, 14 * 1024 * 1024, False),
        ),
    }
    base.update(over)
    return MediaInfo(**base)


def _capability(*, copy_ok: bool, native: tuple[str, ...], converted: tuple[str, ...]) -> Any:
    def build(**_kwargs: Any) -> AudioCapability:
        return AudioCapability(
            formats=tuple(dict.fromkeys((*native, *converted))),
            copy_ok=copy_ok,
            native=native,
            converted=converted,
        )

    return build


def _probe(**facts: Any) -> Any:
    async def probe_media(path: Any) -> MediaFacts:
        return MediaFacts(**facts)

    return probe_media


def _video_result(tmp_path: Any, *, height: int, quality: str) -> DownloadResult:
    job = tmp_path / f"job-{quality}"
    job.mkdir()
    media = job / "clip.mp4"
    media.write_bytes(b"x" * 2048)
    info = MediaInfo(
        source_url=YOUTUBE_URL,
        title="A Clip",
        platform="youtube",
        webpage_url=YOUTUBE_URL,
        extension="mp4",
        thumbnail=None,
        duration=95,
        filesize_approx=2048,
        is_live=False,
        height=height,
    )
    return DownloadResult(
        file_path=media, info=info, media_format="video", quality=quality
    )


def _audio_result(
    tmp_path: Any, *, quality: str, ext: str, source_kbps: int | None = None
) -> DownloadResult:
    job = tmp_path / f"job-{quality}"
    job.mkdir()
    media = job / f"song.{ext}"
    media.write_bytes(b"x" * 2048)
    info = MediaInfo(
        source_url=SOUNDCLOUD_URL,
        title="A Song",
        platform="soundcloud",
        webpage_url=SOUNDCLOUD_URL,
        extension=ext,
        thumbnail=None,
        duration=201,
        filesize_approx=2048,
        is_live=False,
        audio_ext=ext,
        audio_kbps=source_kbps,
    )
    return DownloadResult(
        file_path=media, info=info, media_format="audio", quality=quality
    )


@pytest.fixture(autouse=True)
def _clean_tap_guard() -> Any:
    """Each test starts without the duplicate-tap memory (it is module state)."""
    user_module._recent_requests.clear()
    yield
    user_module._recent_requests.clear()


@pytest.fixture(autouse=True)
def _quiet_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The edges the flow touches that are not under test here: quota reads,
    cache lookups, the preflight verdict — and clean settings in both modules
    that read them."""

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 3, "last_download_date": None}

    def clean_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    monkeypatch.setattr(user_module, "get_settings", clean_settings)
    monkeypatch.setattr(subscription_module, "get_settings", clean_settings)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())


async def _always_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)


# ---------------------------------------------------------------------------
# a. YouTube with multiple qualities
# ---------------------------------------------------------------------------


async def test_a_youtube_link_flows_from_probe_to_verified_caption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    await _always_supported(monkeypatch)
    bot = _bot_with(_info())
    state, queue = _fresh_state(), _fake_queue()

    await user_module._queue_url_flow(
        _message(YOUTUBE_URL, bot),
        state,
        _user(),
        YOUTUBE_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    rows = _buttons(bot.keyboards[-1])
    video_rows = [data for _, data in rows if data.startswith("fmt:video:")]
    assert video_rows == ["fmt:video:1080", "fmt:video:720", "fmt:video:360"], (
        "probe → capability → menu: exactly the rungs the link really has"
    )

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), queue, cast(Bot, bot), lang=EN
    )
    assert len(queue.tasks) == 1
    task = queue.tasks[0]
    assert (task.media_format, task.quality) == ("video", "720"), (
        "callback → task: the validated selection reaches the worker"
    )

    result = _video_result(tmp_path, height=720, quality="720")
    monkeypatch.setattr(
        verify,
        "probe_media",
        _probe(format_name="mp4", codec="h264", width=1280, height=720, media_streams=1),
    )
    mismatch = await verify.verify_produced(
        result.file_path,
        media_format="video",
        quality=task.quality,
        suffix=".mp4",
        produced_p=720,
        selected_p=task.quality,
    )
    assert mismatch is None, "result → verification: the file keeps the selection's promise"

    caption = worker._upload_caption(result, EN, source_url=YOUTUBE_URL)
    assert "720p" in caption and YOUTUBE_URL in caption, "verification → final caption"


# ---------------------------------------------------------------------------
# b. Empty video ladder
# ---------------------------------------------------------------------------


async def test_an_undiscovered_ladder_ends_in_an_honest_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    await _always_supported(monkeypatch)
    bot = _bot_with(_info(video_options=()))
    state, queue = _fresh_state(), _fake_queue()

    await user_module._queue_url_flow(
        _message(YOUTUBE_URL, bot),
        state,
        _user(),
        YOUTUBE_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    assert t("intake.probe_failed", EN) in bot.texts[0]
    rows = _buttons(bot.keyboards[-1])
    assert (t("intake.probe_retry_btn", EN), user_module.PROBE_CALLBACK) in rows
    assert all(not data.startswith("fmt:") for _, data in rows), "no default download stands in"
    assert queue.tasks == [], "and nothing was queued behind the user's back"


# ---------------------------------------------------------------------------
# c. Resolvable Spotify URL
# ---------------------------------------------------------------------------


async def test_a_resolvable_spotify_link_offers_only_deliverable_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(url: str, **kwargs: Any) -> Any:
        return SimpleNamespace(duration_s=201.0)

    monkeypatch.setattr(user_module.spotify, "lookup", lookup)
    monkeypatch.setattr(
        user_module,
        "audio_capability",
        _capability(copy_ok=False, native=(), converted=("mp3", "m4a", "flac")),
    )
    bot = _bot_with(None)
    state, queue = _fresh_state(), _fake_queue()

    await user_module._queue_url_flow(
        _message(SPOTIFY_URL, bot),
        state,
        _user(),
        SPOTIFY_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    assert t("intake.spotify_note", EN) in bot.texts[0], "the resolver is named honestly"
    rows = _buttons(bot.keyboards[-1])
    data = [item for _, item in rows]
    assert "audf:mp3" in data, "a deliverable conversion target is offered"
    assert all(
        item.startswith(("audf:", "fmt:audio:", "menu:")) for item in data
    ), "…and no format the capability model does not contain"
    assert "audf:opus" not in data and "fmt:audio:opus" not in data, (
        "opus is not in this source's deliverable set — it is not advertised"
    )

    await user_module.on_audio_format(_callback(bot, "audf:mp3"), state, lang=EN)
    level_rows = [item for _, item in _buttons(bot.keyboards[-1])]
    chosen = next(item for item in level_rows if item.startswith("fmt:audio:"))

    await user_module.on_format_chosen(
        _callback(bot, chosen), state, _user(), object(), queue, cast(Bot, bot), lang=EN
    )
    assert queue.tasks[-1].media_format == "audio", "menu → callback → task"
    assert queue.tasks[-1].quality == chosen.removeprefix("fmt:audio:")


# ---------------------------------------------------------------------------
# d. Unresolvable Spotify URL
# ---------------------------------------------------------------------------


async def test_an_unresolvable_spotify_link_says_so_and_offers_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(url: str, **kwargs: Any) -> Any:
        raise RuntimeError("no track")

    monkeypatch.setattr(user_module.spotify, "lookup", lookup)
    bot = _bot_with(None)
    state, queue = _fresh_state(), _fake_queue()

    await user_module._queue_url_flow(
        _message(SPOTIFY_URL, bot),
        state,
        _user(),
        SPOTIFY_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    assert t("intake.spotify_unresolved", EN) in bot.texts[0]
    rows = _buttons(bot.keyboards[-1])
    assert (t("intake.probe_retry_btn", EN), user_module.PROBE_CALLBACK) in rows
    assert queue.tasks == [], "no fabricated format grid, no hidden download"


# ---------------------------------------------------------------------------
# e. Stale / expired callback
# ---------------------------------------------------------------------------


async def test_a_stale_or_expired_tap_is_answered_and_never_queues() -> None:
    bot = RecordingBot()
    queue = _fake_queue()

    # A menu predating the current state: the router's stale path answers it.
    await user_module.on_stale_media_tap(_callback(bot, "fmt:video:720"), _user(), lang=EN)
    assert bot.answers[-1].show_alert is True
    assert queue.tasks == []

    # A lost session: the FSM no longer carries the link.
    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), _fresh_state(), _user(), object(), queue, cast(Bot, bot), lang=EN
    )
    assert queue.tasks == [], "an expired selection runs nothing"
    assert bot.answers[-1].show_alert is True, "and says why"


# ---------------------------------------------------------------------------
# f. Selected 720p, delivered 480p
# ---------------------------------------------------------------------------


async def test_selected_720p_delivered_480p_fails_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    await _always_supported(monkeypatch)
    bot = _bot_with(_info())
    state, queue = _fresh_state(), _fake_queue()
    await user_module._queue_url_flow(
        _message(YOUTUBE_URL, bot),
        state,
        _user(),
        YOUTUBE_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )
    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), queue, cast(Bot, bot), lang=EN
    )
    task = queue.tasks[-1]
    assert task.quality == "720"

    # …but the download produced a 480p file (the 720p format vanished).
    result = _video_result(tmp_path, height=480, quality="720")
    monkeypatch.setattr(
        verify, "probe_media", _probe(format_name="mp4", codec="h264", width=854, height=480, media_streams=1)
    )
    recorder = RecordingBot()
    with pytest.raises(ExtractionError) as caught:
        await worker._finish_upload(
            task, cast(Bot, recorder), object(), None, result
        )

    assert caught.value.code == "CONVERSION_MISMATCH"
    assert recorder.calls == [], "nothing was sent — and nothing was captioned 720p"


# ---------------------------------------------------------------------------
# g. Native audio
# ---------------------------------------------------------------------------


async def test_native_audio_is_offered_as_original_and_captioned_as_such(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    await _always_supported(monkeypatch)
    monkeypatch.setattr(
        user_module,
        "audio_capability",
        _capability(copy_ok=True, native=("m4a",), converted=("mp3",)),
    )
    bot = _bot_with(_info(video_options=(), audio_ext="m4a", audio_kbps=130))
    state, queue = _fresh_state(), _fake_queue()
    await user_module._queue_url_flow(
        _message(SOUNDCLOUD_URL, bot),
        state,
        _user(),
        SOUNDCLOUD_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    rows = [item for _, item in _buttons(bot.keyboards[-1])]
    assert "audf:m4a" in rows, "the native codec is on the menu"

    await user_module.on_audio_format(_callback(bot, "audf:m4a"), state, lang=EN)
    level_rows = [item for _, item in _buttons(bot.keyboards[-1])]
    assert "fmt:audio:m4a" in level_rows, "the untouched-stream row exists (copy_ok)"
    assert "fmt:audio:mp3.best" not in level_rows, "this screen is one codec deep"

    await user_module.on_format_chosen(
        _callback(bot, "fmt:audio:m4a"), state, _user(), object(), queue, cast(Bot, bot), lang=EN
    )
    task = queue.tasks[-1]
    assert task.quality == "m4a"

    result = _audio_result(tmp_path, quality="m4a", ext="m4a", source_kbps=130)
    monkeypatch.setattr(
        verify,
        "probe_media",
        _probe(format_name="mov,mp4", codec="aac", bitrate_bps=51_000, media_streams=1),
    )
    assert (
        await verify.verify_produced(
            result.file_path, media_format="audio", quality=task.quality, suffix=".m4a"
        )
        is None
    )
    caption = worker._upload_caption(result, EN, source_url=SOUNDCLOUD_URL)
    assert "M4A · Original" in caption, "the copied stream is captioned as what it is"


# ---------------------------------------------------------------------------
# h. Transcoded audio
# ---------------------------------------------------------------------------


async def test_transcoded_audio_is_captioned_with_its_target_and_source_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    await _always_supported(monkeypatch)
    monkeypatch.setattr(
        user_module,
        "audio_capability",
        _capability(copy_ok=False, native=(), converted=("mp3",)),
    )
    # The probe knows nothing about the source rate (the Spotify shape): the
    # ladder is then offered whole — and the download is where the source's own
    # ≈130 kbps rate turns out to sit under the 320 kbps target.
    bot = _bot_with(_info(video_options=(), audio_ext="m4a", audio_kbps=None))
    state, queue = _fresh_state(), _fake_queue()
    await user_module._queue_url_flow(
        _message(SOUNDCLOUD_URL, bot),
        state,
        _user(),
        SOUNDCLOUD_URL,
        EN,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    await user_module.on_audio_format(_callback(bot, "audf:mp3"), state, lang=EN)
    screen = bot.screens[-1]
    assert t("audio.converted_note", EN) in screen, "a rate row is a conversion target, said so"
    level_rows = [item for _, item in _buttons(bot.keyboards[-1])]
    chosen = "fmt:audio:mp3.best"
    assert chosen in level_rows, "the 320 kbps conversion target is on the ladder"

    await user_module.on_format_chosen(
        _callback(bot, chosen), state, _user(), object(), queue, cast(Bot, bot), lang=EN
    )
    task = queue.tasks[-1]

    result = _audio_result(tmp_path, quality=task.quality, ext="mp3", source_kbps=130)
    monkeypatch.setattr(
        verify,
        "probe_media",
        _probe(format_name="mp3", codec="mp3", bitrate_bps=318_400, media_streams=1),
    )
    assert (
        await verify.verify_produced(
            result.file_path,
            media_format="audio",
            quality=task.quality,
            suffix=".mp3",
            source_kbps=130,
        )
        is None
    )
    caption = worker._upload_caption(result, EN, source_url=SOUNDCLOUD_URL)
    assert "MP3 · 320 kbps" in caption
    assert "from a ≈130 kbps source" in caption, (
        "an upscale is named where the label is read — the caption, not just a log"
    )
    assert "Original" not in caption, "a transcoded file never claims to be native"
