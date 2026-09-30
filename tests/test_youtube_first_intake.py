"""YouTube first intake: the cold probe earns its ladder, and the menu offers audio.

Two production pains, one file. First: a YouTube link sent for the first time
showed no quality ladder (the intake fell back to the retry screen), while the
second send drew the ladder from ``smart_cache`` — the live probe had given up
before every configured client got a fair attempt. Second: the quality menu had
no audio-only row, so a user who wanted just the sound paid for the video.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.i18n import t
from handlers import user as user_module
from services.extractor import (
    AudioCapability,
    ExtractionError,
    ExtractorService,
    MediaInfo,
    VideoOption,
)

USER_ID = 4242
EN = "en"
FA = "fa"
YOUTUBE = "https://youtu.be/abc"
CLIENTS = ("android", "ios", "mweb", "tv", "web")
BLOCKED = ExtractionError("EXTRACTOR_BLOCKED", "سایت مبدأ مسدود کرد")


def _info() -> MediaInfo:
    return MediaInfo(
        source_url=YOUTUBE,
        title="A Clip",
        platform="youtube",
        webpage_url=YOUTUBE,
        extension="mp4",
        thumbnail=None,
        duration=95,
        filesize_approx=None,
        is_live=False,
        video_options=(VideoOption(height=720), VideoOption(height=480)),
    )


def _service(tmp_path: Path) -> ExtractorService:
    return ExtractorService(
        tmp_path,
        js_runtime="none",
        cookie_file=None,
        youtube_clients=CLIENTS,
        retry_attempts=2,
        retry_backoff_s=0,
    )


def _scripted_attempts(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: dict[tuple[str, ...] | None, object],
    steps: list[tuple[tuple[str, ...] | None, bool]],
) -> None:
    """Answer each metadata attempt from ``outcomes``, keyed by its client solo.

    ``None`` is the whole-list attempt; ``(\"mweb\",)`` a raced or solo leg.
    """

    def fake_attempt(
        _self: ExtractorService,
        url: str,
        *,
        youtube_clients: tuple[str, ...] | None = None,
        allow_cookies: bool = True,
    ) -> MediaInfo:
        key = tuple(youtube_clients) if youtube_clients is not None else None
        steps.append((youtube_clients, allow_cookies))
        outcome = outcomes.get(key, BLOCKED)
        if isinstance(outcome, Exception):
            raise outcome
        return cast(MediaInfo, outcome)

    monkeypatch.setattr(ExtractorService, "_extract_attempt", fake_attempt)


# ---------------------------------------------------------------------------
# The cold probe: every configured client gets a fair attempt
# ---------------------------------------------------------------------------


async def test_cold_probe_gives_fallback_clients_a_fair_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raced legs and the whole list fail — a solo fallback still answers.

    ``mweb``/``tv`` lose their race and the full list is refused too; the probe
    must still walk the remaining configured clients one by one (``android``
    answers here) instead of reporting the link unreadable on first contact.
    """
    steps: list[tuple[tuple[str, ...] | None, bool]] = []
    _scripted_attempts(
        monkeypatch,
        {("mweb",): BLOCKED, ("tv",): BLOCKED, None: BLOCKED, ("android",): _info()},
        steps,
    )

    result = await _service(tmp_path)._probe(YOUTUBE)

    assert result.title == "A Clip", "the fallback client's ladder is the menu's ladder"
    soloed = [clients for clients, _ in steps if clients is not None and len(clients) == 1]
    assert ("android",) in soloed, f"android never got its solo attempt: {steps!r}"


async def test_cold_probe_surfaces_the_full_list_error_when_every_client_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No client answers: the probe still fails loudly instead of hanging."""
    steps: list[tuple[tuple[str, ...] | None, bool]] = []
    _scripted_attempts(monkeypatch, {}, steps)

    with pytest.raises(ExtractionError):
        await _service(tmp_path)._probe(YOUTUBE)

    assert steps, "the probe must have tried before failing"


# ---------------------------------------------------------------------------
# The audio-only row on a discovered ladder
# ---------------------------------------------------------------------------


def _ladder() -> tuple[VideoOption, ...]:
    return (VideoOption(height=720), VideoOption(height=480))


def _capability() -> AudioCapability:
    return AudioCapability(formats=("mp3", "m4a", "opus"), copy_ok=True)


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text or "", button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def test_youtube_ladder_carries_an_audio_only_row() -> None:
    """A discovered ladder draws the audio-only button next to the rungs."""
    keyboard = user_module._question_keyboard(
        YOUTUBE, FA, options=_ladder(), capability=_capability()
    )

    rows = _buttons(keyboard)
    assert "fmt:audio:best" in [data for _, data in rows], f"no audio-only row: {rows!r}"
    label = next(text for text, data in rows if data == "fmt:audio:best")
    assert label == t("fmt.audio_best", FA), f"the row must wear its own label: {label!r}"


def test_audio_only_row_needs_a_discovered_ladder() -> None:
    """No ladder, no row: without formats the button would promise nothing."""
    keyboard = user_module._question_keyboard(YOUTUBE, FA, options=(), capability=None)

    assert "fmt:audio:best" not in [data for _, data in _buttons(keyboard)]


def test_audio_best_tap_is_honoured_only_with_a_discovered_ladder() -> None:
    """The tap is valid exactly when the menu drew it — a ladder in state."""
    with_ladder = {
        "offered": ["720", "480"],
        "audio_offered": None,
        "options": [(720, 720, 0, False)],
        "copy_ok": True,
    }
    without_ladder = {"offered": ["best"], "audio_offered": None, "options": []}

    assert user_module._tap_was_offered(YOUTUBE, "audio", "best", with_ladder) is True
    assert user_module._tap_was_offered(YOUTUBE, "audio", "best", without_ladder) is False


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, answers with a real Message."""

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
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
            and call.reply_markup is not None
        ]


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


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


def _user() -> dict[str, Any]:
    return {
        "telegram_id": USER_ID,
        "username": "u",
        "language": FA,
        "is_premium": False,
        "premium_until": None,
    }


class _FakeQueue:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


@pytest.fixture(autouse=True)
def _quiet_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No database, no cache, no preflight, no tap history."""

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 0, "last_download_date": ""}

    async def get_wallet_balance(pool: Any, telegram_id: int) -> int:
        return 0

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    async def supported(url: str) -> bool:
        return True

    class _NoRefusal:
        @staticmethod
        def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
            return type("Verdict", (), {"refused": False, "message": ""})()

    async def ladder_probe(bot: Bot, url: str) -> MediaInfo:
        return _info()

    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    monkeypatch.setattr(user_module.database, "get_wallet_balance", get_wallet_balance)
    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "_probe_supported", supported)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    monkeypatch.setattr(user_module, "_probe_meta", ladder_probe)
    user_module._recent_requests.clear()


async def test_audio_only_tap_queues_best_audio_without_video() -> None:
    """First contact draws the ladder; the audio row queues sound, not video."""
    bot = RecordingBot()
    state = _state()
    queue = _FakeQueue()

    await user_module._queue_url_flow(
        _message(YOUTUBE, bot),
        state,
        _user(),
        YOUTUBE,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=cast(Any, queue),
    )
    first_menu = [data for kb in bot.keyboards for _, data in _buttons(kb)]
    assert "fmt:audio:best" in first_menu, "cold links must draw the audio row immediately"

    await user_module.on_format_chosen(
        _callback(bot, "fmt:audio:best"),
        state,
        _user(),
        object(),
        cast(Any, queue),
        bot,
        lang=FA,
    )

    assert len(queue.tasks) == 1, "the audio tap queues exactly one job"
    assert queue.tasks[0].media_format == "audio"
    assert queue.tasks[0].quality == "best"
