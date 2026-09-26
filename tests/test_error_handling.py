"""Centralized error handling: one handler's crash must never strand the user.

The defect pinned here: with no ``dp.error`` handler, *any* handler exception —
an FSM blip while RedisStorage is unreachable, a database hiccup in the
middleware, a Telegram network error — escapes to the Dispatcher's logger and
nothing else. A callback query goes unanswered (the client spins for half a
minute) and the user gets no feedback at all. The one registered handler logs
the failure with its traceback, stops any spinner, and says the minimum honest
sentence — then lets the bot keep serving everyone else.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from aiogram import Bot, Dispatcher, Router
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from main import on_unhandled_error


class RecordingBot:
    """Records what the error handler sends back at Telegram."""

    #: The FSM middleware and ``feed_update`` read it off every bot.
    id = 7

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True

    async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> Any:
        from aiogram.methods import SendMessage as _Send

        self.calls.append(_Send(chat_id=chat_id, text=text))
        return True


def _message(bot: RecordingBot) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=7, type=cast(Any, "private")),
        from_user=User(id=7, is_bot=False, first_name="u"),
        text="hi",
    ).as_(cast(Bot, bot))


def _crashing_dispatcher() -> Dispatcher:
    """A dispatcher whose one handler always dies — every update's worst day."""
    dp = Dispatcher()
    router = Router()

    @router.message()
    async def boom(message: Message) -> None:
        raise RuntimeError("the handler died")

    @router.callback_query()
    async def boom_tap(callback: CallbackQuery) -> None:
        raise RuntimeError("the tap handler died")

    dp.include_router(router)
    dp.error.register(on_unhandled_error)
    return dp


async def test_a_crashed_handler_still_answers_the_user() -> None:
    bot = RecordingBot()
    dp = _crashing_dispatcher()

    await dp.feed_update(cast(Bot, bot), Update(update_id=1, message=_message(bot)))

    assert any(isinstance(call, SendMessage) for call in bot.calls), (
        "the user hears that something went wrong — never silence"
    )


async def test_a_crashed_handler_releases_the_callback_spinner() -> None:
    bot = RecordingBot()
    dp = _crashing_dispatcher()
    callback = CallbackQuery(
        id="1",
        from_user=User(id=7, is_bot=False, first_name="u"),
        chat_instance="c",
        data="fmt:video:720",
        message=_message(bot),
    ).as_(cast(Bot, bot))

    await dp.feed_update(cast(Bot, bot), Update(update_id=2, callback_query=callback))

    assert any(isinstance(call, AnswerCallbackQuery) for call in bot.calls), (
        "an unanswered callback spins in the client for half a minute"
    )


async def test_the_error_handler_itself_never_raises() -> None:
    """An error handler that raises would replace one unhandled error with
    another — even a feedback channel that cannot send must return cleanly."""
    bot = RecordingBot()

    class _Exploding(RecordingBot):
        async def __call__(self, method: Any) -> Any:
            raise RuntimeError("Telegram is down too")

        async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> Any:
            raise RuntimeError("Telegram is down too")

    exploding = _Exploding()
    exploding.calls = bot.calls
    dp = Dispatcher()
    router = Router()

    @router.message()
    async def boom(message: Message) -> None:
        raise RuntimeError("the handler died")

    dp.include_router(router)
    dp.error.register(on_unhandled_error)

    await dp.feed_update(
        cast(Bot, exploding),
        Update(update_id=3, message=_message(cast(Any, exploding))),
    )
