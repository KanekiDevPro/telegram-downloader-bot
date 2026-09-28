"""The manual card-to-card payment flow, pinned at the money edges.

A buyer who sends a receipt photo has already transferred money; the bot's one
job is to make sure a human admin sees the proof. These tests pin what the buyer
is told when that promise cannot be kept: the receipt is stored, support could
not be reached, and the buyer must learn this here — instead of being thanked
and abandoned with a transaction nobody will ever decide.
"""

from __future__ import annotations

import uuid
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

from core import database
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

    async def attach_receipt(
        self, txn_id: Any, photo_file_id: str, telegram_id: int
    ) -> bool:
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


# ---------------------------------------------------------------------------
# One receipt photo, one decision
# ---------------------------------------------------------------------------

TXN_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
TXN_B = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_USER = 999


class _TxnPool:
    """A transactions table in memory — the attach statement really applies.

    The dispatch also refuses any statement missing its guards (owner, pending,
    one-photo), so these tests fail if the SQL ever loses one.
    """

    def __init__(self) -> None:
        self.rows: dict[Any, dict[str, Any]] = {}
        self.statements: list[str] = []

    def add_txn(
        self, txn_id: Any, *, status: str = "pending", owner: int = USER_ID
    ) -> None:
        self.rows[txn_id] = {
            "id": txn_id,
            "telegram_id": owner,
            "status": status,
            "receipt_photo_id": None,
        }

    async def execute(self, query: str, *args: Any) -> str:
        flat = " ".join(query.split())
        self.statements.append(flat)
        if "SET receipt_photo_id" not in flat:
            return "UPDATE 0"
        assert "telegram_id = $3" in flat, "the owner guard vanished from attach_receipt"
        assert "status = 'pending'" in flat, "the pending guard vanished from attach_receipt"
        assert "NOT EXISTS" in flat, "the one-photo guard vanished from attach_receipt"
        txn_id, photo, owner = args
        row = self.rows.get(txn_id)
        if row is None or row["telegram_id"] != owner or row["status"] != "pending":
            return "UPDATE 0"
        taken = any(
            other["receipt_photo_id"] == photo and other["id"] != txn_id
            for other in self.rows.values()
        )
        if taken:
            return "UPDATE 0"
        row["receipt_photo_id"] = photo
        return "UPDATE 1"


async def test_one_receipt_photo_gets_one_decision_not_n() -> None:
    """P4.2, pinned: N pending transactions could each take the same receipt
    photo — N forwards, N live Approve buttons, and one payment approved twice
    grants the plan twice. One photo now belongs to one transaction."""
    pool = _TxnPool()
    pool.add_txn(TXN_A)
    pool.add_txn(TXN_B)

    assert await database.attach_receipt(cast(Any, pool), TXN_A, "photo-1", USER_ID) is True
    assert await database.attach_receipt(cast(Any, pool), TXN_B, "photo-1", USER_ID) is False
    assert pool.rows[TXN_B]["receipt_photo_id"] is None, "the second one stays empty"
    assert await database.attach_receipt(cast(Any, pool), TXN_B, "photo-2", USER_ID) is True
    assert await database.attach_receipt(cast(Any, pool), TXN_A, "photo-1b", USER_ID) is True


async def test_a_receipt_only_attaches_to_its_senders_pending_transaction() -> None:
    """The owner and pending guards: a stranger's receipt call, or one against
    a decided transaction, changes nothing."""
    pool = _TxnPool()
    pool.add_txn(TXN_A, owner=USER_ID)
    pool.add_txn(TXN_B, status="approved")

    assert await database.attach_receipt(cast(Any, pool), TXN_A, "photo-1", OTHER_USER) is False
    assert await database.attach_receipt(cast(Any, pool), TXN_B, "photo-1", USER_ID) is False
    assert await database.attach_receipt(cast(Any, pool), TXN_A, "photo-1", USER_ID) is True
