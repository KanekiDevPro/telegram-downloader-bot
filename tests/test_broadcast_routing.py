"""The broadcast draft is caught by its own handler — never the text wildcard.

The bug this pins: an admin's announcement text fell into the user router's
generic text fallback ("another step is still in progress"), because the user
router was registered *first* and its wildcard swallowed every message before
the FSM-state handler that asked for ``AdminStates.broadcast`` could see it.
The production dispatch order (``handlers.ROUTERS``, what ``main.py`` registers)
now offers the admin router first — and this file proves it end to end through
a real Dispatcher: a message in the broadcast state is answered by the
broadcast preview, never by the fallback. Commands — ``/cancel`` above all —
keep their own handlers, and the broadcast state is left cleared after a send
(success or failure) and on cancel.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from core.config import Settings
from core.i18n import t
from handlers import ROUTERS
from handlers import admin as admin_module
from handlers import user as user_module
from middlewares import user_middleware as user_mw
from middlewares.user_middleware import UserMiddleware
from services.broadcast import BroadcastReport

ADMIN_ID = 99
FA = "fa"


class RecordingBot:
    """A stand-in for ``Bot``: records what the admin would see."""

    #: What ``FSMContextMiddleware`` keys the storage with.
    id = 42

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True

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


def _message(text: str, bot: RecordingBot) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=data,
        message=_message("panel", bot),
    ).as_(cast(Bot, bot))


def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


@pytest.fixture(autouse=True)
def _admin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ADMIN_IDS`` for the duration of one test (the real one comes from .env)."""

    def settings() -> Settings:
        return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(admin_module, "get_settings", settings)


_wired: Dispatcher | None = None


def _dispatcher() -> Dispatcher:
    """The entrypoint's wiring, built once: ``UserMiddleware`` on every router,
    and the routers in ``handlers.ROUTERS`` order. Once, because aiogram
    routers attach to a single dispatcher — and because the order under test is
    exactly the order the entrypoint registers."""
    global _wired
    if _wired is None:
        middleware = UserMiddleware()
        for r in ROUTERS:
            r.message.middleware(middleware)
            r.callback_query.middleware(middleware)
        dp = Dispatcher(storage=MemoryStorage())
        dp["pool"] = object()
        dp["queue"] = _Queue()
        dp.include_routers(*ROUTERS)
        _wired = dp
    return _wired


class _Queue:
    async def depth(self) -> int:
        return 0

    async def enqueue(self, task: Any) -> int:
        return 0


async def _feed(
    text: str, *, state: Any, monkeypatch: pytest.MonkeyPatch
) -> tuple[RecordingBot, FSMContext]:
    """One update through the *production* dispatch order, with the given FSM state."""
    async def get_or_create_user(
        pool: Any, telegram_id: int, username: str | None, language: str
    ) -> dict[str, Any]:
        return {
            "telegram_id": telegram_id,
            "username": username,
            "language": FA,
            "is_premium": False,
            "premium_until": None,
        }

    async def count_users(pool: Any) -> int:
        return 10

    monkeypatch.setattr(user_mw.database, "get_or_create_user", get_or_create_user)
    monkeypatch.setattr(admin_module.database, "count_users", count_users)

    def settings() -> Settings:
        return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(user_mw, "get_settings", settings)

    dp = _dispatcher()
    bot = RecordingBot()
    context = dp.fsm.resolve_context(
        bot=cast(Bot, bot), chat_id=ADMIN_ID, user_id=ADMIN_ID
    )
    assert context is not None
    await context.clear()  # each test owns its own state and data
    if state is not None:
        await context.set_state(state)

    update = Update(
        update_id=1,
        message=Message(
            message_id=10,
            date=datetime.now(timezone.utc),
            chat=Chat(id=ADMIN_ID, type="private"),
            from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
            text=text,
        ),
    )
    await dp.feed_update(cast(Bot, bot), update)
    return bot, context


# ---------------------------------------------------------------------------
# The routing contract
# ---------------------------------------------------------------------------


def test_the_admin_router_is_offered_before_the_text_wildcard() -> None:
    """The order the entrypoint registers — the bug was the reverse of this."""
    assert ROUTERS.index(admin_module.router) < ROUTERS.index(user_module.router)


async def test_the_broadcast_text_is_caught_by_the_state_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complaint, pinned end to end: an admin's announcement lands in the
    broadcast draft handler (preview + confirm), and the generic text fallback
    never answers it."""
    bot, context = await _feed(
        "announcement text", state=admin_module.AdminStates.broadcast, monkeypatch=monkeypatch
    )

    assert t("admin.broadcast_preview", FA, users=10) in bot.screens, (
        "the broadcast draft handler caught the text"
    )
    assert t("intake.step_in_progress", FA) not in bot.screens, (
        "the generic text fallback never saw it"
    )
    data = await context.get_data()
    assert data.get("announcement") == "announcement text", "and kept it as the draft"


async def test_cancel_during_a_broadcast_still_clears_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/cancel`` is a command — it keeps its own handler even with the admin
    router first (a command must never become draft text)."""
    bot, context = await _feed(
        "/cancel", state=admin_module.AdminStates.broadcast, monkeypatch=monkeypatch
    )

    assert t("intake.cancelled", FA) in bot.screens
    assert await context.get_state() is None, "the broadcast state is cleared"
    assert t("intake.step_in_progress", FA) not in bot.screens


# ---------------------------------------------------------------------------
# The state is always cleared
# ---------------------------------------------------------------------------


async def test_the_broadcast_state_is_cleared_after_a_send_success_or_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success, then an exception: both leave nothing behind. The draft is read
    and the state cleared *before* the first message goes out, so even a send
    that dies mid-run cannot leave an admin trapped in the flow."""
    bot = RecordingBot()
    state = _fsm()
    await state.set_state(admin_module.AdminStates.broadcast)
    await state.update_data(announcement="hi")

    async def deliver(bot_: Any, pool: Any, announcement: str, on_progress: Any = None) -> Any:
        return BroadcastReport(total=3, sent=3, blocked=0, failed=0)

    monkeypatch.setattr(admin_module.broadcast, "deliver", deliver)
    await admin_module.on_broadcast_send(
        _callback(bot, admin_module.BC_SEND), state, cast(Bot, bot), object(), lang=FA
    )
    assert await state.get_state() is None, "cleared after a successful send"

    bot2 = RecordingBot()
    state2 = _fsm()
    await state2.set_state(admin_module.AdminStates.broadcast)
    await state2.update_data(announcement="hi")

    async def boom(bot_: Any, pool: Any, announcement: str, on_progress: Any = None) -> Any:
        raise RuntimeError("the send died")

    monkeypatch.setattr(admin_module.broadcast, "deliver", boom)
    await admin_module.on_broadcast_send(
        _callback(bot2, admin_module.BC_SEND), state2, cast(Bot, bot2), object(), lang=FA
    )
    assert await state2.get_state() is None, "cleared even when the send raises"
    assert t("admin.broadcast_failed", FA) in bot2.screens


async def test_the_broadcast_state_is_cleared_by_cancel() -> None:
    bot = RecordingBot()
    state = _fsm()
    await state.set_state(admin_module.AdminStates.broadcast)
    await state.update_data(announcement="hi")

    await admin_module.on_broadcast_cancel(
        _callback(bot, admin_module.BC_CANCEL), state, lang=FA
    )

    assert await state.get_state() is None
    assert t("admin.broadcast_cancelled", FA) in bot.screens
