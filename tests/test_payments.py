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
from aiogram.methods import EditMessageCaption, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User

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
        self.methods: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.methods.append(method)
        if isinstance(method, SendMessage):
            self.texts.append(method.text or "")
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.texts.append(text)
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
    def __init__(self, pool: Any = None) -> None:
        self.attached: list[tuple[str, str]] = []
        self.begins = 0
        self.decisions: list[tuple[str, bool]] = []
        self._pool = pool

    async def begin(self, user: Any, plan: Any) -> Any:
        self.begins += 1
        return self._pool.mint(int(plan["id"]), int(plan["price"]))

    async def attach_receipt(
        self, txn_id: Any, photo_file_id: str, telegram_id: int
    ) -> bool:
        self.attached.append((str(txn_id), photo_file_id))
        return True

    async def decide(self, txn_id: Any, approved: bool) -> Any:
        self.decisions.append((str(txn_id), approved))
        return {"telegram_id": USER_ID}


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


# ---------------------------------------------------------------------------
# One tap, one transaction
# ---------------------------------------------------------------------------


class _PlanPool:
    """A transactions + plans table in memory: ``begin`` really stores rows."""

    def __init__(self) -> None:
        self.rows: dict[Any, dict[str, Any]] = {}
        self._next = 0

    def mint(self, plan_id: int, amount: int) -> dict[str, Any]:
        self._next += 1
        txn_id = uuid.UUID(int=self._next)
        row = {
            "id": txn_id,
            "telegram_id": USER_ID,
            "plan_id": plan_id,
            "amount": amount,
            "status": "pending",
            "receipt_photo_id": None,
        }
        self.rows[txn_id] = row
        return row

    async def fetchrow(self, query: str, *args: Any) -> Any:
        flat = " ".join(query.split())
        if "FROM transactions WHERE id" in flat:
            return self.rows.get(args[0])
        if "FROM subscription_plans WHERE id" in flat:
            return {"id": args[0], "name": "month", "price": 50000}
        return None


def _clean_settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _tap(bot: RecordingBot) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer"),
        chat_instance="chat",
        data="plan:1",
        message=_message(bot),
    ).as_(cast(Bot, bot))


async def _tap_plan(
    pool: _PlanPool, strategy: FakeStrategy, bot: RecordingBot, state: FSMContext
) -> None:
    await payment_module.on_plan_selected(
        _tap(bot),
        state,
        cast(Any, pool),
        cast(Any, SimpleNamespace(get=lambda _method: strategy)),
        cast(Any, {"telegram_id": USER_ID}),
        lang=EN,
    )


def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


async def test_tapping_a_plan_twice_mints_one_transaction_not_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P4.3, pinned: every tap minted a fresh pending transaction and pointed
    the FSM at it — the buyer read the first card, paid that amount, and the
    receipt attached to the second transaction, orphaning the first. Selection
    is idempotent now: the open card is re-sent, never duplicated."""
    monkeypatch.setattr(payment_module, "get_settings", _clean_settings)
    pool = _PlanPool()
    strategy = FakeStrategy(pool)
    bot = RecordingBot()
    state = _fsm()

    await _tap_plan(pool, strategy, bot, state)
    await _tap_plan(pool, strategy, bot, state)

    assert strategy.begins == 1, "the second tap mints nothing"
    assert len(pool.rows) == 1, "one purchase, one transaction"
    assert bot.texts and len(bot.texts) == 2 and bot.texts[0] == bot.texts[1], (
        "the same card is re-sent — same plan, same amount"
    )
    data = await state.get_data()
    assert data["txn_id"] == str(next(iter(pool.rows))), "and the FSM still points at it"


async def test_a_decided_transaction_lets_the_next_tap_mint_a_fresh_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotence is for *open* purchases: once the admin decided, the next
    tap is a new purchase and says so with its own transaction."""
    monkeypatch.setattr(payment_module, "get_settings", _clean_settings)
    pool = _PlanPool()
    strategy = FakeStrategy(pool)
    bot = RecordingBot()
    state = _fsm()

    await _tap_plan(pool, strategy, bot, state)
    next(iter(pool.rows.values()))["status"] = "approved"
    await _tap_plan(pool, strategy, bot, state)

    assert strategy.begins == 2
    assert len(pool.rows) == 2


async def test_the_re_sent_card_names_the_open_transaction_not_the_tapped_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-sent card is the *purchase's* card: its amount is the one the
    buyer would pay, even if the plan's price has changed since."""
    monkeypatch.setattr(payment_module, "get_settings", _clean_settings)
    pool = _PlanPool()
    strategy = FakeStrategy(pool)
    bot = RecordingBot()
    state = _fsm()

    await _tap_plan(pool, strategy, bot, state)
    next(iter(pool.rows.values()))["amount"] = 10  # this purchase's price
    await _tap_plan(pool, strategy, bot, state)

    assert strategy.begins == 1
    assert len(pool.rows) == 1
    card = bot.texts[-1]
    assert "10" in card, "the open transaction's amount"
    assert "50,000" not in card, "not the tapped plan's current price"


# ---------------------------------------------------------------------------
# The decision ends the receipt's interactivity
# ---------------------------------------------------------------------------


def _admin_settings() -> Settings:
    return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]


def _captioned_message(bot: RecordingBot) -> Message:
    """The admin's receipt card — a photo message, so it has a caption to edit."""
    return Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=ADMIN_ID, type=cast(Any, "private")),
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
        caption="Receipt #1",
    ).as_(cast(Bot, bot))


async def test_a_decision_strips_the_approval_keyboard_from_the_receipt(
    monkeypatch: pytest.MonkeyPatch,
    _offline: FakeStrategy,
) -> None:
    """P4.1, pinned: the verdict's caption edit carried no ``reply_markup``, and
    the Bot API keeps the existing keyboard when it is omitted — so every admin
    kept live Approve/Reject buttons on a transaction that refuses any second
    decision, and a stray tap could only be answered with a confusing alert.
    The decision empties the keyboard the moment it lands."""
    monkeypatch.setattr(payment_module, "get_settings", _admin_settings)
    strategy = _offline
    bot = RecordingBot()
    cb = CallbackQuery(
        id="1",
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=f"txn_approve:{TXN_ID}",
        message=_captioned_message(bot),
    ).as_(cast(Bot, bot))

    await payment_module.on_admin_approve(
        cb,
        cast(Bot, bot),
        cast(Any, object()),
        cast(Any, SimpleNamespace(get=lambda _method: strategy)),
        lang=EN,
    )

    assert strategy.decisions == [(TXN_ID, True)], "the transaction was decided once"
    edits = [m for m in bot.methods if isinstance(m, EditMessageCaption)]
    assert edits, "the receipt card was rewritten with the verdict"
    markup = edits[0].reply_markup
    assert markup is not None and markup.inline_keyboard == [], (
        "the Approve/Reject keyboard must not survive the decision"
    )
