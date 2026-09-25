"""One screen, one message: a media message is rewritten as its caption.

Telegram has no edit that turns a media message into a text message, so a photo
carrying a menu cannot be re-screened with ``edit_text`` — the old behaviour
answered with a *second* message instead, which is exactly the message pile the
one-message UI exists to prevent. These tests pin the rule: a media message's
screen lives in its caption and is edited there; a text message is edited as
text; and the two refusals keep their two answers — "not modified" is silence,
anything else may still fall back to a fresh reply (only ``edit_quietly``, for
flow updates whose action has already happened, swallows silently).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageText, SendMessage
from aiogram.types import Chat, InlineKeyboardMarkup, LinkPreviewOptions, Message, PhotoSize, User

from core.ui import edit_or_reply, edit_quietly

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


class RecordingBot:
    """A stand-in for ``Bot``: records what was sent, and can refuse an edit."""

    def __init__(self, *, refusing: str = "") -> None:
        self.calls: list[Any] = []
        self.refusing = refusing  # an error text every edit is answered with

    async def __call__(self, method: Any) -> Any:
        if self.refusing and isinstance(method, (EditMessageText, EditMessageCaption)):
            raise TelegramBadRequest(method=cast("Any", method), message=self.refusing)
        self.calls.append(method)
        return True

    @property
    def texts(self) -> list[Any]:
        return [call for call in self.calls if isinstance(call, SendMessage)]


def _message(bot: RecordingBot, *, photo: bool) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, is_bot=False, first_name="u"),
        text=None if photo else "old screen",
        caption="old caption" if photo else None,
        photo=(
            [PhotoSize(file_id="p", file_unique_id="u", width=1, height=1)] if photo else None
        ),
    ).as_(cast(Bot, bot))


def _keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


async def test_a_media_screen_is_rewritten_as_its_caption() -> None:
    """The photo stays the message — its caption becomes the new screen."""
    bot = RecordingBot()

    await edit_or_reply(
        _message(bot, photo=True),
        "new screen",
        reply_markup=_keyboard(),
        link_preview_options=NO_PREVIEW,
    )

    assert len(bot.calls) == 1, "one edit — never an edit and a message"
    edit = bot.calls[0]
    assert isinstance(edit, EditMessageCaption)
    assert edit.caption == "new screen"
    assert bot.texts == [], "the redundant plain-text message is gone"


async def test_a_caption_edit_carries_no_link_preview_option() -> None:
    """Captions have no link previews — a text-only kwarg must not travel there."""
    bot = RecordingBot()

    await edit_or_reply(
        _message(bot, photo=True), "new screen", link_preview_options=NO_PREVIEW
    )

    edit = bot.calls[0]
    assert getattr(edit, "link_preview_options", None) is None


async def test_an_unchanged_caption_is_answered_with_silence() -> None:
    """"Message is not modified" means the screen is already up — nothing follows."""
    bot = RecordingBot(refusing="Bad Request: message is not modified: same content")

    await edit_or_reply(_message(bot, photo=True), "new screen")

    assert bot.calls == [] and bot.texts == [], "silence, not a duplicate"


async def test_a_caption_telegram_will_not_edit_still_gets_a_fresh_reply() -> None:
    """A menu that fails silently is worse than one extra message."""
    bot = RecordingBot(refusing="Bad Request: message can't be edited")

    await edit_or_reply(_message(bot, photo=True), "new screen")

    assert [call.text for call in bot.texts] == ["new screen"]


async def test_a_text_message_is_still_edited_as_text() -> None:
    bot = RecordingBot()

    await edit_or_reply(_message(bot, photo=False), "new screen", reply_markup=_keyboard())

    edit = bot.calls[0]
    assert isinstance(edit, EditMessageText)
    assert edit.text == "new screen"
    assert bot.texts == []


async def test_quiet_flow_updates_rewrite_captions_too() -> None:
    """Where the action has already happened, the rewrite is best-effort."""
    bot = RecordingBot()
    await edit_quietly(_message(bot, photo=True), "new screen")
    assert isinstance(bot.calls[0], EditMessageCaption)

    refusing = RecordingBot(refusing="Bad Request: message can't be edited")
    await edit_quietly(_message(refusing, photo=True), "new screen")
    assert refusing.calls == [] and refusing.texts == [], "a quiet update never replies"
