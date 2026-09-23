"""The two things every screen needs: which message to edit, and how to edit it.

The bot's screens are one message edited in place rather than a stream of new
ones, so every callback handler starts the same way ("give me the message behind
this tap") and ends the same way ("rewrite it, or answer somewhere else when
Telegram will not"). Those two moves live here instead of being re-typed per
module — not a framework, just the shared half-sentence.

:class:`Screen` makes one rule explicit: a screen is its text *and* its keyboard,
always replaced together. There is no half-rendered state where the previous
screen's buttons survive under new text — showing a screen swaps both in one
edit. Where a screen leads back to is therefore not extra state either: it is
the one ``⬅️`` button every keyboard is built with (its destination is the
*parent* screen's callback).

``edit_or_reply`` exists because Telegram refuses edits to messages that are too
old or unchanged, and the two refusals deserve different answers: a message that
cannot be edited at all gets a fresh reply (a menu that fails silently is worse
than one extra message), while "message is not modified" means the screen the
user asked for is *already up* — re-sending it would be the duplicate-message
spam the one-message UI exists to avoid, so nothing is sent.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


class Screen(NamedTuple):
    """One page of the UI: its whole text and its whole keyboard, as a pair.

    A two-tuple on purpose — handlers unpack it (``text, keyboard = screen``)
    and Telegram receives both in a single ``edit_text``, which is what makes
    every navigation feel like entering a page rather than rewriting a caption.
    """

    text: str
    keyboard: InlineKeyboardMarkup


def callback_message(cb: CallbackQuery) -> Message | None:
    """The message behind a tap — or ``None`` when Telegram no longer has one.

    A forwarded keyboard, an ``InaccessibleMessage`` or a cleaned-up chat all
    arrive as callbacks without a message a bot may edit. Handlers treat that as
    a normal case (answer with an alert), not a crash.
    """
    message = cb.message
    return message if isinstance(message, Message) else None


async def edit_or_reply(message: Message, text: str, **kwargs: Any) -> None:
    """Update a message in place; one that cannot be edited gets a fresh reply.

    Identical content is answered with silence: the requested screen is already
    on the screen, and a second copy of it is precisely the message pile this
    module exists to prevent.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await message.answer(text, **kwargs)


async def show_screen(message: Message, screen: Screen) -> None:
    """Replace whatever this message was with ``screen`` — text and buttons.

    The whole point of the type: there is no way to swap one without the other.
    """
    await edit_or_reply(message, screen.text, reply_markup=screen.keyboard)


async def edit_quietly(message: Message, text: str, **kwargs: Any) -> None:
    """Rewrite a message, swallowing refusals — for flow updates, not menus.

    Where the *action* behind the button has already happened (a broadcast is
    running, a probe finished) a failed status rewrite is not worth interrupting
    anyone over, and a fallback reply here would bury the flow under noise.
    """
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest:
        pass
