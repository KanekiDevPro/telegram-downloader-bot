"""TikTok auto-best and the Back-from-preview glitch, pinned.

A TikTok video holds a single file behind a login wall — exactly like an
Instagram reel — so asking a quality question about it is a menu of guesses.
The link goes straight to the best download, the way Instagram already does.

And a question asked as a photo (the link's own thumbnail) must not haunt the
menu it came from: tapping Back from a media preview deletes the preview and
brings the previous menu as fresh text, instead of caption-editing the old
photo into a menu-with-a-picture.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    DeleteMessage,
    EditMessageCaption,
    EditMessageText,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User

from core import ui as ui_module
from handlers import user as user_module

USER_ID = 4242
EN = "en"
TIKTOK_URL = "https://www.tiktok.com/@user/video/123"


class RecordingBot:
    def __init__(self, *, delete_refused: bool = False) -> None:
        self.calls: list[Any] = []
        self.delete_refused = delete_refused
        self.state: Any = SimpleNamespace()

    async def __call__(self, method: Any) -> Any:
        if self.delete_refused and isinstance(method, DeleteMessage):
            raise TelegramBadRequest(method=method, message="Bad Request: no delete rights")
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return _text_message(method.text or "", self)
        return True

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def edits(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, EditMessageText)]


def _text_message(text: str, bot: RecordingBot) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        text=text,
    ).as_(cast(Bot, bot))


def _photo_message(bot: RecordingBot) -> Message:
    return Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        text=None,
        caption="the question, under a thumbnail",
        photo=[PhotoSize(file_id="p", file_unique_id="u", width=640, height=360)],
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, message: Message) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=message,
    ).as_(cast(Bot, bot))


def _fresh_state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


class FakeQueue:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return len(self.tasks)

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


def _user() -> Any:
    return {
        "telegram_id": USER_ID,
        "username": "ali",
        "is_premium": False,
        "premium_until": None,
        "language": EN,
        "is_new": False,
    }


# ---------------------------------------------------------------------------
# TikTok auto-best
# ---------------------------------------------------------------------------


async def test_a_tiktok_video_goes_straight_to_best(monkeypatch: pytest.MonkeyPatch) -> None:
    """No quality ladder for a TikTok video: one file, one automatic request."""

    async def supported(url: str) -> bool:
        return True

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 0, "last_download_date": None}

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    user_module._recent_requests.clear()
    bot = RecordingBot()
    state, queue = _fresh_state(), FakeQueue()

    await user_module._queue_url_flow(
        _text_message(TIKTOK_URL, bot),
        state,
        _user(),
        TIKTOK_URL,
        EN,
        bot=cast(Bot, bot),
        pool=cast(Any, object()),
        queue=cast(Any, queue),
    )

    assert len(queue.tasks) == 1, "asked nothing — queued directly"
    assert (queue.tasks[0].media_format, queue.tasks[0].quality) == ("video", "best")
    assert await state.get_state() is None, "no format question is left open"


# ---------------------------------------------------------------------------
# Back from a media preview
# ---------------------------------------------------------------------------


async def test_back_from_a_photo_question_deletes_the_preview() -> None:
    """The Download screen arrives as text — the thumbnail stays behind."""
    bot = RecordingBot()

    await user_module.on_menu_download(
        _callback(bot, "menu:download", _photo_message(bot)), _fresh_state(), pool=None, lang=EN
    )

    assert any(isinstance(call, DeleteMessage) for call in bot.calls), "the photo goes"
    assert not any(isinstance(call, EditMessageCaption) for call in bot.calls), (
        "never a caption-edit: the menu must not wear the old picture"
    )
    assert bot.texts != [], "the picker is re-sent as fresh text"


async def test_home_back_from_a_photo_deletes_the_preview() -> None:
    bot = RecordingBot()

    await user_module.on_menu_home(
        _callback(bot, "menu:home", _photo_message(bot)),
        _fresh_state(),
        _user(),
        pool=None,
        lang=EN,
    )

    assert any(isinstance(call, DeleteMessage) for call in bot.calls)
    assert not any(isinstance(call, EditMessageCaption) for call in bot.calls)
    assert bot.texts != [], "Home arrives as its own message"


async def test_back_from_a_text_screen_still_edits_in_place() -> None:
    """No media, no delete: the one-message UI keeps editing the same message."""
    bot = RecordingBot()

    await user_module.on_menu_download(
        _callback(bot, "menu:download", _text_message("old", bot)), _fresh_state(), pool=None, lang=EN
    )

    assert not any(isinstance(call, DeleteMessage) for call in bot.calls)
    assert bot.texts == [], "edited, not re-sent"
    assert bot.edits != []


async def test_back_without_delete_rights_still_navigates() -> None:
    """A chat that refuses the delete still gets its menu — as a caption."""
    bot = RecordingBot(delete_refused=True)

    await user_module.on_menu_download(
        _callback(bot, "menu:download", _photo_message(bot)), _fresh_state(), pool=None, lang=EN
    )

    assert any(isinstance(call, EditMessageCaption) for call in bot.calls), (
        "the fallback navigates instead of stranding the user"
    )


async def test_reset_to_text_edits_a_text_message() -> None:
    bot = RecordingBot()

    await ui_module.reset_to_text(_text_message("old", bot), "new screen")

    assert bot.edits == ["new screen"]
    assert bot.texts == []
    assert not any(isinstance(call, DeleteMessage) for call in bot.calls)
