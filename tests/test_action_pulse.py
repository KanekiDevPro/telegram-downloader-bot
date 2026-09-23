"""Chat-action pulses: the «it's alive» indicator that is not a message.

The two-message contract says the chat holds the card and the media and nothing
else — so progress is a *chat action* (Telegram's ephemeral typing/upload
indicator), sent on a heartbeat, dead with the work: success, failure and
cancellation all stop it, and a short job never flickers one at all.
"""
from __future__ import annotations

import pytest
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest

from services.delivery import ActionPulse, upload_action


class FakeBot:
    def __init__(self, *, refusing: bool = False) -> None:
        self.actions: list[tuple[int, ChatAction]] = []
        self.refusing = refusing

    async def send_chat_action(self, chat_id: int, action: ChatAction) -> None:
        if self.refusing:
            raise TelegramBadRequest(method=None, message="CHAT_ACTION_FORBIDDEN")  # type: ignore[arg-type]
        self.actions.append((chat_id, action))


async def test_the_pulse_keeps_the_indicator_lit_for_long_work() -> None:
    bot = FakeBot()
    async with ActionPulse(bot, -100, ChatAction.UPLOAD_VIDEO, grace=0, heartbeat=0.01):  # type: ignore[arg-type]
        await _settle(0.05)
    assert len(bot.actions) >= 2
    assert all(action == ChatAction.UPLOAD_VIDEO for _chat, action in bot.actions)


async def test_a_short_job_never_flickers_the_indicator() -> None:
    """Inside the grace window the pulse is simply never seen — by design."""
    bot = FakeBot()
    async with ActionPulse(bot, 5, grace=0.25, heartbeat=0.01):  # type: ignore[arg-type]
        await _settle(0.01)
    assert bot.actions == []


async def test_the_pulse_stops_the_moment_the_work_ends() -> None:
    bot = FakeBot()
    async with ActionPulse(bot, 5, grace=0, heartbeat=0.01):  # type: ignore[arg-type]
        await _settle(0.05)
    frozen = len(bot.actions)
    await _settle(0.06)
    assert len(bot.actions) == frozen, "the heartbeat survived its work"


async def test_a_failure_still_stops_the_pulse() -> None:
    bot = FakeBot()
    with pytest.raises(RuntimeError, match="download died"):
        async with ActionPulse(bot, 5, grace=0, heartbeat=0.01):  # type: ignore[arg-type]
            await _settle(0.03)
            raise RuntimeError("download died")
    frozen = len(bot.actions)
    await _settle(0.06)
    assert len(bot.actions) == frozen


async def test_stopping_is_idempotent() -> None:
    pulse = ActionPulse(FakeBot(), 5, grace=0)  # type: ignore[arg-type]
    await pulse.__aenter__()
    await pulse.stop()
    await pulse.stop()  # a second stop is nothing at all


async def test_a_chat_that_refuses_actions_does_not_break_the_job() -> None:
    bot = FakeBot(refusing=True)
    async with ActionPulse(bot, 5, grace=0, heartbeat=0.01):  # type: ignore[arg-type]
        await _settle(0.03)  # no exception escapes the pulse
    assert bot.actions == []


async def test_the_default_indicator_is_typing() -> None:
    bot = FakeBot()
    async with ActionPulse(bot, 5, grace=0, heartbeat=1):  # type: ignore[arg-type]
        await _settle(0.02)
    assert bot.actions and bot.actions[0][1] == ChatAction.TYPING


def test_the_upload_indicator_matches_the_delivery_shape() -> None:
    assert upload_action("video") == ChatAction.UPLOAD_VIDEO
    assert upload_action("audio") == ChatAction.UPLOAD_VOICE
    assert upload_action("photo") == ChatAction.UPLOAD_PHOTO
    assert upload_action("photo_group") == ChatAction.UPLOAD_PHOTO
    assert upload_action("file") == ChatAction.UPLOAD_DOCUMENT
    assert upload_action("nonsense") == ChatAction.UPLOAD_DOCUMENT


async def _settle(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
