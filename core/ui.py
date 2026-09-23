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

import secrets
import time
from typing import Any, NamedTuple

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.i18n import t


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


# ---------------------------------------------------------------------------
# Retry: the one action a failed download offers
# ---------------------------------------------------------------------------

#: The callback prefix a retry button carries: ``retry:<key>``.
RETRY_PREFIX = "retry:"

#: How long a retry button keeps working. A failure is worth another try for
#: about as long as the chat it landed in is on screen — a day is generous
#: without letting dead payloads pile up.
RETRY_TTL_S = 24 * 60 * 60

#: Cap on remembered payloads (oldest dropped first): this is a convenience
#: store behind button taps, not a job history — the queue owns those.
_RETRY_CAP = 500

#: Callback key → ``(expires at, owner, the payload a retry rebuilds a job from)``.
_retries: dict[str, tuple[float, int, dict[str, Any]]] = {}


def remember_retry(
    payload: dict[str, Any], *, owner: int, now: float | None = None
) -> str:
    """Remember one failed job behind a fresh key; the key travels in the button.

    The payload is server-side state, not callback data: what a retry re-runs is
    decided here, and a crafted callback can at most name a key that exists. The
    ``owner`` is whose job it is — nobody else's tap can even spend it.
    """
    moment = time.monotonic() if now is None else now
    for key, (expires, _, _) in list(_retries.items()):
        if expires <= moment:
            _retries.pop(key, None)
    while len(_retries) >= _RETRY_CAP:
        _retries.pop(next(iter(_retries)))
    key = secrets.token_urlsafe(9)
    _retries[key] = (moment + RETRY_TTL_S, owner, dict(payload))
    return key


def take_retry(
    key: str, *, owner: int | None = None, now: float | None = None
) -> dict[str, Any] | None:
    """The payload behind ``key`` — exactly once, and only for its owner.

    A second tap finds nothing (the job already restarted). Somebody else's tap
    finds nothing *and leaves the key alone*: a crafted callback must not be able
    to spend a stranger's retry.
    """
    moment = time.monotonic() if now is None else now
    entry = _retries.get(key)
    if entry is None:
        return None
    expires, holder, payload = entry
    if expires <= moment:
        _retries.pop(key, None)
        return None
    if owner is not None and holder != owner:
        return None
    _retries.pop(key, None)
    return payload


def retry_keyboard(key: str, lang: str) -> InlineKeyboardMarkup:
    """``[🔄 Try again] [⬅️ Back]`` — a failure screen's whole vocabulary.

    Back goes to the Download screen: that is where a retry's alternative lives
    (another link, or the same one at a different quality), and it is the parent
    every question in this flow hangs off.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=t("media.retry", lang), callback_data=f"{RETRY_PREFIX}{key}")
    builder.button(text=t("menu.back", lang), callback_data="menu:download")
    builder.adjust(1)  # side by side they wrap on a phone — one action per row
    return builder.as_markup()
