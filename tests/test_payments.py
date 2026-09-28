"""The manual card-to-card payment flow, pinned at the money edges.

A buyer who sends a receipt photo has already transferred money; the bot's one
job is to make sure a human admin sees the proof. These tests pin what the buyer
is told when that promise cannot be kept: the receipt is stored, support could
not be reached, and the buyer must learn this here — instead of being thanked
and abandoned with a transaction nobody will ever decide.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, PhotoSize, User

from core.config import Settings
from core.i18n import t
from handlers import payment as payment_module

USER_ID = 4242
ADMIN_ID = 777
EN = "en"
TXN_ID = "3f1c6a1e-1f64-4c2f-9a1e-1f644c2f9a1e"


class RecordingBot:
    """A stand-in for ``Bot``: the admin forward can be made to fail on demand
    while every buyer-facing answer is recorded for assertion."""

    def __init__(self, *, deliverable: bool = True) -> None:
        self.deliverable = deliverable
        self.forwards: list[int] = []
        self.texts: list[str] = []

    async def __call__(self, method: Any) -> Any:
        if isinstance(method, SendMessage):
            self.texts.append(method.text or "")
        return True

    async def send_photo(self, admin_id: int, photo: str, **kwargs: Any) -> Any:
        self.forwards.append(admin_id)
        if not self.deliverable:
            raise RuntimeError("telegram is down")
        return True


def _message(bot: RecordingBot) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type=cast(Any, "private")),
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer"),
        photo=[PhotoSize(file_id="receipt-photo", file_unique_id="uniq", width=1, height=1)],
    ).as_(cast(Bot, bot))


class FakeStrategy:
    def __init__(self) -> None:
        self.attached: list[tuple[str, str]] = []

    async def attach_receipt(self, txn_id: Any, photo_file_id: str) -> bool:
        self.attached.append((str(txn_id), photo_file_id))
        return True


async def _waiting_state() -> FSMContext:
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    await state.set_state(payment_module.PaymentStates.waiting_receipt)
    await state.update_data(txn_id=TXN_ID)
    return state


@pytest.fixture
def _offline(monkeypatch: pytest.MonkeyPatch) -> FakeStrategy:
    """Everything past the photo is faked: the transaction, the plan and the
    admin list. Settings are built from scratch (``_env_file=None``) so the
    machine's own ``.env`` can never decide who receives a receipt."""
    strategy = FakeStrategy()

    async def get_transaction(pool: Any, txn_id: Any) -> Any:
        return {"plan_id": 1, "amount": 50000}

    async def get_plan(pool: Any, plan_id: Any) -> Any:
        return {"name": "month"}

    async def targets(pool: Any, ids: Any, *, fallback: str = "") -> list[tuple[int, str]]:
        return [(ADMIN_ID, EN)]

    def clean_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr(payment_module.database, "get_transaction", get_transaction)
    monkeypatch.setattr(payment_module.database, "get_plan", get_plan)
    monkeypatch.setattr(payment_module.recipients, "targets", targets)
    monkeypatch.setattr(payment_module, "get_settings", clean_settings)
    return strategy


async def _send_receipt(bot: RecordingBot, strategy: FakeStrategy) -> None:
    payment_service = SimpleNamespace(get=lambda _method: strategy)
    await payment_module.on_receipt_photo(
        _message(bot),
        await _waiting_state(),
        cast(Any, object()),
        cast(Any, payment_service),
        cast(Bot, bot),
        lang=EN,
    )


async def test_a_receipt_no_admin_received_still_tells_the_buyer_the_truth(
    _offline: FakeStrategy,
) -> None:
    """P1.1, pinned: when every admin forward fails the buyer has paid real
    money and must be told support was never reached — never thanked as if an
    admin held the receipt. The receipt itself is stored, so the wording points
    at retrying or contacting support instead of panicking the buyer."""
    bot = RecordingBot(deliverable=False)

    await _send_receipt(bot, _offline)

    assert bot.forwards == [ADMIN_ID]
    assert t("pay.receipt_unreachable", EN) in bot.texts
    assert t("pay.receipt_received", EN) not in bot.texts


async def test_a_receipt_that_reached_an_admin_is_reported_received(
    _offline: FakeStrategy,
) -> None:
    """The other branch stays honest the other way: one delivered forward and
    the buyer keeps the receipt-received promise they had before."""
    bot = RecordingBot(deliverable=True)

    await _send_receipt(bot, _offline)

    assert bot.forwards == [ADMIN_ID]
    assert bot.texts == [t("pay.receipt_received", EN)]
