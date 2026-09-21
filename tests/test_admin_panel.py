"""The admin panel: what it answers, who may open it, and where its buttons go.

Two different promises. What an operator *reads*: real numbers from the running
stack, and a health line per dependency that says whether it answered rather than
raising when one of them is down — a panel that breaks when something is broken is
the one screen that must never break. And who may read it: ``ADMIN_IDS`` and nobody
else, including for a forwarded message whose buttons travel with it.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.config import Settings
from handlers import admin as admin_module
from services import panel as panel_module

ADMIN_ID = 99
STRANGER_ID = 7


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True

    @property
    def screens(self) -> list[str]:
        """Everything the admin would see, in order (new messages and edits alike)."""
        return [
            call.text or ""
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
        ]

    @property
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
            and call.reply_markup is not None
        ]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]


def _message(text: str, bot: RecordingBot, user_id: int = ADMIN_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, user_id: int = ADMIN_ID) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=data,
        message=_message("panel", bot, user_id),
    ).as_(cast(Bot, bot))


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


class _Queue:
    def __init__(self, depth: int = 4, *, broken: bool = False) -> None:
        self.depth_value = depth
        self.broken = broken

    async def depth(self) -> int:
        if self.broken:
            raise ConnectionError("redis is down")
        return self.depth_value


class _Cobalt:
    enabled = True
    available = True


def _queue(**kwargs: Any) -> Any:
    """The stub, typed loosely: the panel functions declare the real queue class,
    and this stands in for it (it answers ``depth()`` and nothing else)."""
    return _Queue(**kwargs)


def _cobalt() -> Any:
    return _Cobalt()


@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADMIN_IDS`` for the duration of one test (the real one comes from ``.env``).

    Replaced rather than cached: the panel asks for settings on every screen and on
    every button, so a stub that builds a fresh value is both simpler and honest
    about the fact that nothing here is memoized.
    """

    def settings() -> Settings:
        return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(admin_module, "get_settings", settings)


@pytest.fixture
def stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """One row of numbers, and a language mix, without a database."""
    row = {
        "users": 120,
        "premium": 9,
        "new_users": 3,
        "active_today": 14,
        "downloads_today": 41,
        "cache_rows": 88,
        "blocks_24h": 5,
        "pending_txns": 2,
    }

    async def admin_stats(pool: Any, today: Any) -> dict[str, Any]:
        return row

    async def language_counts(pool: Any) -> list[dict[str, Any]]:
        return [{"language": "fa", "count": 90}, {"language": "en", "count": 30}]

    monkeypatch.setattr(panel_module.database, "admin_stats", admin_stats)
    monkeypatch.setattr(panel_module.database, "language_counts", language_counts)


@pytest.fixture
def healthy(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The dependencies answering — the stub is the answer, not the network."""

    class _Pool:
        async def fetchval(self, query: str) -> int:
            return 1

    async def fallback_health(settings: Any, cobalt: Any, *, probe: bool, pool: Any = None) -> Any:
        from services.doctor import FallbackHealth

        return FallbackHealth(state="ready", url="http://cobalt:9000", dialect="v10", seconds=0.2)

    async def reachable(url: str, timeout: float = 5.0) -> bool:
        return True

    monkeypatch.setattr(panel_module, "fallback_health", fallback_health)
    monkeypatch.setattr(panel_module, "http_reachable", reachable)
    return _Pool()


# ---------------------------------------------------------------------------
# Who may open it
# ---------------------------------------------------------------------------


async def test_the_panel_opens_for_an_admin(stats: dict[str, Any]) -> None:
    bot = RecordingBot()

    await admin_module.cmd_admin(_message("/admin", bot), object(), _queue(), None)

    assert "پنل مدیریت" in bot.screens[0] or "Admin panel" in bot.screens[0]
    assert "120" in bot.screens[0], "the numbers are the point"
    assert _buttons(bot.keyboards[0])[0][1] == "admin:stats"


async def test_a_stranger_is_not_shown_the_numbers(stats: dict[str, Any]) -> None:
    bot = RecordingBot()

    await admin_module.cmd_admin(_message("/admin", bot, STRANGER_ID), object(), _queue(), None)

    assert len(bot.screens) == 1
    assert "120" not in bot.screens[0], "the numbers never reach a non-admin"
    assert bot.keyboards == [], "and neither does the panel"


async def test_a_forwarded_panel_button_is_still_admin_only(stats: dict[str, Any]) -> None:
    """The buttons travel with a forwarded message, so the check is repeated on the
    callback rather than trusted from the command that drew the keyboard."""
    bot = RecordingBot()
    cb = _callback(bot, "admin:stats", STRANGER_ID)

    await admin_module.on_panel_button(cb, object(), _queue(), None)

    assert bot.answers and bot.answers[0].show_alert is True
    assert bot.screens == [], "nothing is rendered"


# ---------------------------------------------------------------------------
# What it says
# ---------------------------------------------------------------------------


async def test_every_screen_renders_for_an_admin(stats: dict[str, Any], healthy: Any) -> None:
    for screen in ("home", "stats", "health", "queue", "tools"):
        text, keyboard = await admin_module.panel_screen(
            screen, healthy, _queue(), _cobalt(), lang="fa"
        )

        assert text.strip(), screen
        assert _buttons(keyboard), screen


async def test_the_stats_screen_shows_every_number_it_collects(stats: dict[str, Any]) -> None:
    text = await panel_module.stats_text(object(), "en")

    for value in ("120", "9", "3", "41", "14", "88", "5", "2"):
        assert value in text, value
    assert "fa: 90" in text and "en: 30" in text


async def test_the_health_screen_reports_the_fallback_and_both_helpers(healthy: Any) -> None:
    text = await panel_module.health_text(object(), _queue(), Settings(_env_file=None), _cobalt(), "en")  # type: ignore[call-arg]

    assert "ready" in text
    assert "cobalt:9000" in text and "v10" in text
    assert "PO-token provider" in text and "YouTube session server" in text
    assert "online" in text


async def test_a_dead_dependency_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The health screen is the one screen that must not break when something is
    broken — an operator opens it *because* something is."""
    monkeypatch.setattr(panel_module, "fallback_health", _raising_fallback)
    monkeypatch.setattr(panel_module, "http_reachable", _unreachable)

    class DeadPool:
        async def fetchval(self, query: str) -> int:
            raise ConnectionError("postgres is down")

    text = await panel_module.health_text(
        DeadPool(), _queue(broken=True), Settings(_env_file=None), None, "en"  # type: ignore[call-arg]
    )

    assert "unreachable" in text
    assert "the probe itself failed" in text


async def _raising_fallback(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("no instance configured")


async def _unreachable(url: str, timeout: float = 5.0) -> bool:
    return False


async def test_the_queue_screen_survives_redis_being_down() -> None:
    text = await panel_module.queue_text(_queue(broken=True), Settings(_env_file=None), "en")  # type: ignore[call-arg]

    assert "?" in text, "an unknown depth, not a crash"


async def test_the_tools_screen_points_at_the_tools_that_already_exist() -> None:
    text, keyboard = await admin_module.panel_screen(
        "tools", object(), _queue(), None, lang="en"
    )

    from services.cookie_watch import DOCTOR_CALLBACK, REFRESH_CALLBACK

    destinations = [data for _, data in _buttons(keyboard)]
    assert DOCTOR_CALLBACK in destinations and REFRESH_CALLBACK in destinations
    assert "/doctor" in text and "/refresh" in text, "the commands still work too"


async def test_the_panel_can_be_read_in_persian_or_english(stats: dict[str, Any]) -> None:
    persian = await panel_module.stats_text(object(), "fa")
    english = await panel_module.stats_text(object(), "en")

    assert "آمار" in persian and "Stats" in english


@pytest.mark.parametrize(
    ("state", "expected"),
    (
        ("ready", "ready"),
        ("quarantined", "quarantined"),
        ("auth", "needs an API key"),
        ("unreachable", "unreachable"),
        ("off", "off"),
    ),
)
def test_each_fallback_state_has_a_readable_name(state: str, expected: str) -> None:
    assert expected in panel_module.cobalt_state_text(state, "en")


def test_an_unknown_state_is_shown_as_it_is() -> None:
    """Better a word an operator has never seen than a silent blank."""
    assert panel_module.cobalt_state_text("something-new", "en") == "something-new"


# ---------------------------------------------------------------------------
# The buttons
# ---------------------------------------------------------------------------


def test_every_panel_button_has_somewhere_to_go() -> None:
    source = inspect.getsource(admin_module)
    offered = {"admin:stats", "admin:health", "admin:queue", "admin:tools", "admin:home"}
    offered |= {data for _, data in _buttons(admin_module._tools_keyboard("en"))}

    from services.cookie_watch import DOCTOR_CALLBACK, REFRESH_CALLBACK

    missing = [
        data
        for data in sorted(offered - {DOCTOR_CALLBACK, REFRESH_CALLBACK})
        if f'"{data}"' not in source
    ]
    assert missing == [], missing


def test_the_panel_buttons_are_all_answered() -> None:
    source = inspect.getsource(admin_module.on_panel_button)
    assert ".answer(" in source


async def test_pressing_a_button_rewrites_the_message(
    stats: dict[str, Any], healthy: Any
) -> None:
    bot = RecordingBot()
    cb = _callback(bot, "admin:queue")

    await admin_module.on_panel_button(cb, healthy, _queue(depth=7), _cobalt(), lang="en")

    assert bot.answers and bot.answers[0].text is None, "answered, not an alert"
    assert "7" in bot.screens[0], "the depth that was just read"
    assert len(bot.screens) == 1, "edited in place: one screen, not a stack of them"


async def test_an_unknown_panel_screen_falls_back_to_the_home_screen(
    stats: dict[str, Any],
) -> None:
    bot = RecordingBot()
    cb = _callback(bot, "admin:something-new")

    await admin_module.on_panel_button(cb, object(), _queue(), None, lang="en")

    assert "Admin panel" in bot.screens[0]
