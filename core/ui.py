"""The two things every screen needs: which message to edit, and how to edit it.

The bot's screens are one message edited in place rather than a stream of new
ones, so every callback handler starts the same way ("give me the message behind
this tap") and ends the same way ("rewrite it, or answer somewhere else when
Telegram will not"). Those two moves live here instead of being re-typed per
module — not a framework, just the shared half-sentence.

``edit_or_reply`` exists because Telegram refuses edits to messages that are too
old or unchanged, and a menu that fails silently is worse than a fresh message:
the fallback is a send.
"""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message


def callback_message(cb: CallbackQuery) -> Message | None:
    """The message behind a tap — or ``None`` when Telegram no longer has one.

    A forwarded keyboard, an ``InaccessibleMessage`` or a cleaned-up chat all
    arrive as callbacks without a message a bot may edit. Handlers treat that as
    a normal case (answer with an alert), not a crash.
    """
    message = cb.message
    return message if isinstance(message, Message) else None


async def edit_or_reply(message: Message, text: str, **kwargs: Any) -> None:
    """Update a message in place; one that cannot be edited gets a fresh reply."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        await message.answer(text, **kwargs)
