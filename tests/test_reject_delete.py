"""Wrong-source rejection deletes the rejected user message (Track A).

The mode gate already rejects incompatible URLs; this pins the UX follow-up:
the rejected user message itself is removed (fire-and-forget, never fatal),
the warning is still sent, the mode is untouched, and valid links never take
this path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage
from aiogram.types import Chat, Message, User

from handlers import user as user_module
from handlers.user import DownloadStates
from services import download_mode

USER_ID = 4242
EN = "en"

SPOTIFY_URL = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
TIKTOK_URL = "https://www.tiktok.com/@username/video/7200000000000000000"


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return _message(method.text or "", self)
        return True

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def deletes(self) -> list[DeleteMessage]:
        return [call for call in self.calls if isinstance(call, DeleteMessage)]


def _message(text: str, bot: RecordingBot, user_id: int = USER_ID) -> Message:
    return Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=cast(Any, "private")),
        from_user=User(id=user_id, is_bot=False, first_name="user"),
        text=text,
    ).as_(cast(Bot, bot))


def _fresh_state(user_id: int = USER_ID) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _user(user_id: int = USER_ID) -> Any:
    return {
        "telegram_id": user_id,
        "username": "ali",
        "is_premium": False,
        "premium_until": None,
        "language": EN,
        "is_new": False,
    }


class FakeQueue:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return len(self.tasks)

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    download_mode.reset_for_tests()
    user_module._recent_requests.clear()
    user_module._pending_deletes.clear()
    yield
    download_mode.reset_for_tests()
    user_module._recent_requests.clear()
    user_module._pending_deletes.clear()


def _stub_intake_tail(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {"accepted": [], "probed": [], "cached": []}

    async def fake_ask(
        message: Message,
        state: FSMContext,
        user: Any,
        url: str,
        lang: str,
        **kwargs: Any,
    ) -> None:
        seen["accepted"].append(url)
        await state.set_state(DownloadStates.waiting_format)
        await state.update_data(url=url)

    async def fake_cached_rows(pool: Any, url: str) -> list[Any]:
        seen["cached"].append(url)
        return []

    async def fake_probe_supported(url: str) -> bool:
        seen["probed"].append(url)
        return True

    async def fake_canonical(url: str) -> str:
        return url

    async def fake_disabled(pool: Any) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(user_module, "_ask_about_link", fake_ask)
    monkeypatch.setattr(user_module, "_cached_rows", fake_cached_rows)
    monkeypatch.setattr(user_module, "_probe_supported", fake_probe_supported)
    monkeypatch.setattr(user_module, "_canonical_url", fake_canonical)
    monkeypatch.setattr(user_module, "_disabled_platforms", fake_disabled)
    return seen


async def _reject(
    bot: RecordingBot,
    state: FSMContext,
    queue: FakeQueue,
    mode: str,
    url: str,
    message: Message | None = None,
) -> None:
    await state.update_data({download_mode.MODE_KEY: mode})
    download_mode.begin_session(USER_ID)
    await user_module._intake_flow(
        message or _message(url, bot),
        state,
        _user(),
        url,
        EN,
        bot=cast(Bot, bot),
        pool=None,
        queue=cast(Any, queue),
    )
    await user_module.drain_pending_deletes()


async def test_spotify_mode_rejected_youtube_message_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

    await _reject(bot, state, queue, download_mode.SPOTIFY, YOUTUBE_URL)

    assert [d.message_id for d in bot.deletes] == [7]
    assert any("Spotify" in text for text in bot.texts)


async def test_youtube_mode_rejected_spotify_message_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

    await _reject(bot, state, queue, download_mode.YOUTUBE, SPOTIFY_URL)

    assert [d.message_id for d in bot.deletes] == [7]
    assert any("YouTube" in text for text in bot.texts)


async def test_generic_mode_rejected_spotify_message_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

    await _reject(bot, state, queue, download_mode.GENERIC, SPOTIFY_URL)

    assert [d.message_id for d in bot.deletes] == [7]
    assert bot.texts != []


async def test_deletion_failure_never_crashes_and_warning_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

    sent: list[str] = []

    class SimpleRefusingMessage:
        async def answer(self, text: str, **kwargs: Any) -> Message:
            sent.append(text)
            return _message(text, bot)

        async def delete(self) -> None:
            raise RuntimeError("no delete rights")

    await state.update_data({download_mode.MODE_KEY: download_mode.SPOTIFY})
    download_mode.begin_session(USER_ID)
    await user_module._intake_flow(
        cast(Any, SimpleRefusingMessage()),
        state,
        _user(),
        YOUTUBE_URL,
        EN,
        bot=cast(Bot, bot),
        pool=None,
        queue=cast(Any, queue),
    )
    await user_module.drain_pending_deletes()

    assert any("Spotify" in text for text in sent)


async def test_valid_url_is_not_rejected_and_takes_no_reject_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await state.update_data({download_mode.MODE_KEY: download_mode.SPOTIFY})
    download_mode.begin_session(USER_ID)

    await user_module._intake_flow(
        _message(SPOTIFY_URL, bot),
        state,
        _user(),
        SPOTIFY_URL,
        EN,
        bot=cast(Bot, bot),
        pool=None,
        queue=cast(Any, queue),
    )

    assert seen["accepted"] == [SPOTIFY_URL]
    assert not any("Spotify download mode" in text for text in bot.texts)


async def test_rejection_has_no_side_effects_and_keeps_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

    await _reject(bot, state, queue, download_mode.SPOTIFY, YOUTUBE_URL)

    assert queue.tasks == []
    assert seen["cached"] == []
    assert seen["probed"] == []
    data = await state.get_data()
    assert download_mode.normalize(data.get(download_mode.MODE_KEY)) == download_mode.SPOTIFY
