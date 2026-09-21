"""The user-facing frontend: the menu, the profile, premium, help, and the wait.

Three promises are pinned here. The menu has one home and a way back to it (every
screen is the *same* message, edited). The profile answers the four questions a
user actually asks — who am I here, which plan am I on, how much is left, what is
running. And nothing that costs a wait happens silently: the moment a link or a
format choice arrives, the bot says what it is doing, in the message that will
carry the outcome.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import Settings
from core.utils import today_local
from handlers import payment as payment_module
from handlers import user as user_module
from handlers.user import DownloadStates
from services import subscription as subscription_module

USER_ID = 4242


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
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def edits(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, EditMessageText)]

    @property
    def screens(self) -> list[str]:
        """Everything the user would see, in order (new messages and edits alike)."""
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


def _callback(bot: RecordingBot, data: str, *, stale: bool = False) -> CallbackQuery:
    """A callback bound to a recording bot; ``stale`` = Telegram sent no message
    (an ``InaccessibleMessage``, or nothing at all).

    Built with ``message=None`` from the start, because the model is frozen — the
    same reason the handlers have to treat an inaccessible message as a real case
    rather than an edge one.
    """
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=None if stale else _message("menu", bot),
    ).as_(cast(Bot, bot))


def _buttons(markup: Any) -> list[tuple[str, str]]:
    """``[(label, callback_data)]`` for every button on that screen."""
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _user(*, premium: bool = False, until: datetime | None = None, username: str | None = "ali") -> Any:
    return {
        "telegram_id": USER_ID,
        "username": username,
        "is_premium": premium,
        "premium_until": until,
    }


class FakeQueue:
    """Only the two queue facts the gateway needs (it never downloads)."""

    def __init__(self, depth: int = 0) -> None:
        self.depth_value = depth
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return self.depth_value

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return self.depth_value


@pytest.fixture(autouse=True)
def _quiet_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quota reads: three downloads used today, clean settings, no plans.

    Settings are built from scratch (``_env_file=None``) in *both* modules that
    read them, so the machine's own ``.env`` can never decide what these tests
    assert about quotas or plan prices.
    """
    today = today_local()

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 3, "last_download_date": today}

    def clean_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    monkeypatch.setattr(user_module, "get_settings", clean_settings)
    monkeypatch.setattr(subscription_module, "get_settings", clean_settings)


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


def test_the_menu_offers_profile_premium_and_help() -> None:
    markup = user_module._main_menu()

    labels = dict(_buttons(markup))
    assert labels == {
        "👤 پروفایل من": "menu:profile",
        "💎 ارتقا به ویژه (VIP)": "menu:premium",
        "❓ راهنما": "menu:help",
    }
    assert len(markup.inline_keyboard) == 3, "one button per row: these labels are long"


async def test_start_shows_the_menu() -> None:
    bot = RecordingBot()
    message = _message("/start", bot)

    await user_module.cmd_start(message, _user())

    assert "سلام" in bot.texts[0] and "لینک" in bot.texts[0]
    assert _buttons(bot.keyboards[0]) == _buttons(user_module._main_menu())


async def test_every_screen_has_a_way_back() -> None:
    for screen in (
        user_module._back_to_menu(),
        user_module._back_to_menu(),
    ):
        assert ("🔙 بازگشت", "menu:home") in _buttons(screen)


async def test_back_returns_to_the_menu_in_the_same_message() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:home")

    await user_module.on_menu_home(cb, _user())

    assert bot.answers and bot.answers[0].text is None, "it is not an alert, just an answer"
    assert len(bot.edits) == 1 and "سلام" in bot.edits[0], "edited in place"
    assert bot.texts == [], "and nothing new was sent"
    assert _buttons(bot.keyboards[-1]) == _buttons(user_module._main_menu())


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


async def test_the_profile_shows_id_username_status_and_quota() -> None:
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(message, _user(), object(), FakeQueue(depth=2))

    text = bot.texts[0]
    assert "پروفایل من" in text
    assert str(USER_ID) in text, "the Telegram id, so support can identify the account"
    assert "@ali" in text
    assert "رایگان 🪙" in text
    assert "سهمیهٔ امروز: 3 از 10" in text and "7 باقی مانده" in text
    assert "کارهای در صف: 2" in text
    assert ("🔙 بازگشت", "menu:home") in _buttons(bot.keyboards[-1])


async def test_a_premium_profile_says_so_with_the_days_left() -> None:
    until = datetime.now(timezone.utc) + timedelta(days=12, hours=1)
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(message, _user(premium=True, until=until), object(), FakeQueue())

    text = bot.texts[0]
    assert "ویژه 💎" in text and "رایگان" not in text
    assert "12 روز مانده" in text
    assert "60" in text, "premium quota, not the free one"


async def test_a_username_less_user_is_not_shown_as_blank() -> None:
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(message, _user(username=None), object(), FakeQueue())

    assert "نام کاربری: —" in bot.texts[0]


async def test_the_profile_button_edits_the_menu_into_the_profile() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:profile")

    await user_module.on_menu_profile(cb, _user(), object(), FakeQueue())

    assert [answer.text for answer in bot.answers] == [None], "answered exactly once"
    assert len(bot.edits) == 1 and "پروفایل من" in bot.edits[0]


async def test_a_stale_profile_callback_answers_with_an_alert() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:profile", stale=True)

    await user_module.on_menu_profile(cb, _user(), object(), FakeQueue())

    assert len(bot.answers) == 1 and bot.answers[0].show_alert is True
    assert bot.edits == []


# ---------------------------------------------------------------------------
# Premium
# ---------------------------------------------------------------------------


async def test_the_premium_screen_offers_the_plans_and_a_way_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def plans(pool: Any, *, back: bool = False) -> Any:
        builder = InlineKeyboardBuilder()
        builder.button(text="VIP — 50,000 تومان", callback_data="plan:7")
        if back:
            builder.button(text="🔙 بازگشت", callback_data="menu:home")
        builder.adjust(1)
        return builder.as_markup()

    monkeypatch.setattr(user_module, "plans_keyboard", plans)
    bot = RecordingBot()
    cb = _callback(bot, "menu:premium")

    await user_module.on_menu_premium(cb, object())

    assert "ویژه (VIP)" in bot.edits[0]
    assert "10 → <b>60</b>" in bot.edits[0], "the actual quota change, from settings"
    assert _buttons(bot.keyboards[-1]) == [
        ("VIP — 50,000 تومان", "plan:7"),
        ("🔙 بازگشت", "menu:home"),
    ]
    assert len(bot.answers) == 1


async def test_premium_without_any_plan_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_plans(pool: Any, *, back: bool = False) -> None:
        return None

    monkeypatch.setattr(user_module, "plans_keyboard", no_plans)
    bot = RecordingBot()
    message = _message("/premium", bot)

    await user_module.cmd_premium(message, object())

    assert bot.texts == [payment_module.NO_PLANS_TEXT]


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


async def test_help_explains_the_flow_and_lists_the_commands() -> None:
    bot = RecordingBot()
    message = _message("/help", bot)

    await user_module.cmd_help(message)

    text = bot.texts[0]
    assert "لینک رو بفرست" in text
    assert "MP3" in text
    for command in ("/profile", "/premium", "/status", "/cancel", "/start"):
        assert command in text, command
    assert ("🔙 بازگشت", "menu:home") in _buttons(bot.keyboards[-1])


async def test_the_help_button_edits_the_menu_into_the_help() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:help")

    await user_module.on_menu_help(cb)

    assert "راهنما" in bot.edits[0]
    assert len(bot.answers) == 1


def test_no_menu_button_is_left_without_a_handler() -> None:
    """Every ``menu:`` button must have somewhere to go (a dead button is worse
    than no button at all)."""
    source = inspect.getsource(user_module)
    offered = {data for _, data in _buttons(user_module._main_menu())}
    offered |= {data for _, data in _buttons(user_module._back_to_menu())}

    missing = [data for data in sorted(offered) if f'F.data == "{data}"' not in source]
    assert missing == [], missing


# ---------------------------------------------------------------------------
# Immediate feedback, and the queueing path
# ---------------------------------------------------------------------------


async def _state() -> FSMContext:
    context = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    await context.set_state(DownloadStates.waiting_format)
    await context.update_data(url="https://youtu.be/abc")
    return context


async def test_a_link_is_acknowledged_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot = RecordingBot()
    message = _message("https://youtu.be/abc", bot)

    await user_module._queue_url_flow(message, await _state(), object(), _user(), "https://youtu.be/abc")

    assert bot.texts[0] == user_module._ANALYSING, "said first, before the probe"
    assert "چی می‌خوای؟" in bot.edits[-1], "and the same message becomes the question"
    assert ("🎬 ویدیو (بهترین کیفیت)", "fmt:video") in _buttons(bot.keyboards[-1])


async def test_an_unsupported_link_answers_in_the_same_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unsupported(url: str) -> bool:
        return False

    monkeypatch.setattr(user_module, "_probe_supported", unsupported)
    bot = RecordingBot()
    message = _message("https://example.com/x", bot)

    await user_module._queue_url_flow(message, await _state(), object(), _user(), "https://example.com/x")

    assert bot.texts == [user_module._ANALYSING]
    assert "پشتیبانی نمی‌شود" in bot.edits[-1]


async def test_choosing_a_format_shows_the_queueing_step_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_cache(pool: Any, url: str, media_format: str) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    cb = _callback(bot, "fmt:video")
    queue = FakeQueue(depth=1)

    await user_module.on_format_chosen(cb, await _state(), _user(), object(), queue, bot)

    assert bot.texts[0] == user_module._QUEUEING, "the tap is acknowledged instantly"
    assert "موقعیت تقریبی: 1" in bot.edits[-1]
    assert len(bot.answers) == 1 and bot.answers[0].text is None
    assert len(queue.tasks) == 1 and queue.tasks[0].media_format == "video"


async def test_an_exhausted_quota_is_answered_with_an_alert_and_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_cache(pool: Any, url: str, media_format: str) -> None:
        return None

    async def used_up(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 10, "last_download_date": today_local()}

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module.database, "get_daily_usage", used_up)
    bot = RecordingBot()
    cb = _callback(bot, "fmt:audio")

    await user_module.on_format_chosen(cb, await _state(), _user(), object(), FakeQueue(), bot)

    assert "سهمیهٔ دانلود امروزت (10 از 10) تمام شده" in bot.edits[-1]
    assert len(bot.answers) == 1 and bot.answers[0].show_alert is True


class _NoRefusal:
    """``preflight`` with nothing to say (the real rules have their own tests)."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return _Verdict()


class _Verdict:
    refused = False
    message = ""


#: Handlers that answer through a shared function instead of doing it themselves.
_ANSWERS_VIA: dict[str, str] = {
    "on_admin_approve": "_admin_decide",
    "on_admin_reject": "_admin_decide",
}


def test_every_registered_callback_answers() -> None:
    """A callback that is never answered leaves a spinner on the user's button.

    The two admin-approval handlers answer through one shared decision function
    (that is the point of it), so the delegate is followed explicitly rather than
    pretending every handler does its own. Any handler added later must answer
    itself.
    """
    from handlers import admin as admin_module

    missing: list[str] = []
    for module, router in (
        (user_module, user_module.router),
        (payment_module, payment_module.router),
        (admin_module, admin_module.router),
    ):
        for handler in router.callback_query.handlers:
            name = getattr(handler.callback, "__name__", "?")
            source = inspect.getsource(handler.callback)
            delegate = _ANSWERS_VIA.get(name)
            if delegate is not None:
                source += inspect.getsource(getattr(module, delegate))
            if ".answer(" not in source:
                missing.append(name)
    assert missing == [], missing


def test_the_old_menu_callbacks_are_gone() -> None:
    """The menu was rearranged; the buttons it used to offer must not linger as
    dead endpoints."""
    offered = {data for _, data in _buttons(user_module._main_menu())}
    assert not offered & {"menu:download", "menu:status", "menu:subscribe"}
