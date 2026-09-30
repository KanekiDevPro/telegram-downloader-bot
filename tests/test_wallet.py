"""Profile wallet integration: top-up entry on Profile, pay-with-balance at checkout.

The Profile screen must not duplicate Home's support route — it carries the
wallet top-up instead. And a buyer whose internal balance covers a plan must be
offered an explicit pay-with-balance tap that debits atomically and grants the
plan, instead of being forced through the manual receipt flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User

from core import database
from core.i18n import t
from handlers import payment as payment_module
from handlers import user as user_module

EN = "en"
FA = "fa"
USER_ID = 4242


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text or "", button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _message(bot: Any) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type=cast(Any, "private")),
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer"),
    ).as_(cast(Bot, bot))


class RecordingBot:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.keyboards: list[Any] = []
        self.alerts: list[str] = []

    async def __call__(self, method: Any) -> Any:
        from aiogram.methods import SendMessage

        if isinstance(method, SendMessage):
            self.texts.append(method.text or "")
            if method.reply_markup is not None:
                self.keyboards.append(method.reply_markup)
        return SimpleNamespace(message_id=11)


def _tap(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer"),
        chat_instance="chat",
        data=data,
        message=_message(bot),
    ).as_(cast(Bot, bot))


# ---------------------------------------------------------------------------
# Profile screen: top-up in, support out
# ---------------------------------------------------------------------------


def test_the_profile_carries_topup_instead_of_support() -> None:
    offered = dict(_buttons(user_module._profile_keyboard(FA)))

    assert offered["💳 شارژ کیف پول"] == "menu:topup"
    assert "menu:support" not in offered.values()


def test_an_admins_profile_carries_topup_instead_of_support() -> None:
    offered = dict(_buttons(user_module._profile_keyboard(FA, admin=True)))

    assert offered["💳 شارژ کیف پول"] == "menu:topup"
    assert "menu:support" not in offered.values()
    assert "menu:premium" not in offered.values()


def test_the_support_route_still_lives_on_home_only() -> None:
    home = dict(_buttons(user_module._main_menu(FA)))
    profile = dict(_buttons(user_module._profile_keyboard(FA)))

    assert home["ℹ️ راهنما و پشتیبانی"] == "menu:support"
    assert "menu:support" not in profile.values()


# ---------------------------------------------------------------------------
# Wallet storage: credit, atomic debit
# ---------------------------------------------------------------------------


class _WalletPool:
    """A users table in memory — the debit statement really applies."""

    def __init__(self, balance: int = 0) -> None:
        self.balances: dict[int, int] = {USER_ID: balance}
        self.statements: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        flat = " ".join(query.split())
        self.statements.append(flat)
        if "FROM users" in flat and "wallet_balance" in flat:
            return {"wallet_balance": self.balances.get(args[0], 0)}
        if "SET wallet_balance" in flat and "wallet_balance -" in flat:
            assert "wallet_balance >=" in flat, "the funds guard vanished from debit_wallet"
            assert "RETURNING wallet_balance" in flat, "the debit must return the new balance"
            telegram_id, amount = args[0], int(args[1])
            if self.balances.get(telegram_id, 0) < amount:
                return None
            self.balances[telegram_id] -= amount
            return {"wallet_balance": self.balances[telegram_id]}
        if "SET wallet_balance" in flat and "wallet_balance +" in flat:
            telegram_id, amount = args[0], int(args[1])
            self.balances[telegram_id] = self.balances.get(telegram_id, 0) + amount
            return {"wallet_balance": self.balances[telegram_id]}
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append(" ".join(query.split()))
        return "UPDATE 1"


async def test_a_debit_with_funds_lands_and_reports_the_new_balance() -> None:
    pool = _WalletPool(balance=100_000)

    assert await database.debit_wallet(cast(Any, pool), USER_ID, 50_000) == 50_000
    assert pool.balances[USER_ID] == 50_000


async def test_a_debit_without_funds_changes_nothing() -> None:
    pool = _WalletPool(balance=10_000)

    assert await database.debit_wallet(cast(Any, pool), USER_ID, 50_000) is None
    assert pool.balances[USER_ID] == 10_000, "a short wallet is not touched"


async def test_credit_accumulates() -> None:
    pool = _WalletPool(balance=10_000)

    assert await database.add_wallet_credit(cast(Any, pool), USER_ID, 40_000) == 50_000


# ---------------------------------------------------------------------------
# Checkout: pay with the wallet when it covers the plan
# ---------------------------------------------------------------------------


class _CheckoutPool(_WalletPool):
    def __init__(self, balance: int) -> None:
        super().__init__(balance)
        self.granted: list[tuple[int, int]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        flat = " ".join(query.split())
        if "FROM subscription_plans WHERE id" in flat:
            return {"id": 1, "name": "month", "duration_days": 30, "price": 50_000}
        return await super().fetchrow(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        flat = " ".join(query.split())
        if "SET is_premium" in flat:
            assert "make_interval" in flat, "the grant must extend premium by the plan"
            self.granted.append((args[0], int(args[1])))
            return "UPDATE 1"
        return await super().execute(query, *args)


def _buyer(telegram_id: int = USER_ID) -> dict[str, Any]:
    return {"telegram_id": telegram_id, "username": "buyer", "language": EN}


async def _select_plan(bot: RecordingBot, pool: _CheckoutPool) -> None:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    async def _begin(user: Any, plan: Any) -> Any:
        return {
            "id": "3f1c6a1e-1f64-4c2f-9a1e-1f644c2f9a1e",
            "plan_id": 1,
            "amount": 50_000,
            "status": "pending",
        }

    service = SimpleNamespace(get=lambda _method: SimpleNamespace(begin=_begin))
    await payment_module.on_plan_selected(
        _tap(bot, "plan:1"),
        state,
        cast(Any, pool),
        cast(Any, service),
        cast(Any, _buyer()),
        lang=EN,
    )


async def test_a_funded_wallet_is_offered_at_checkout() -> None:
    bot = RecordingBot()

    await _select_plan(bot, _CheckoutPool(balance=80_000))

    assert bot.keyboards, "the card must carry a keyboard with the wallet tap"
    offered = [data for _, data in _buttons(bot.keyboards[-1])]
    assert "pay_wallet:1" in offered
    assert any(
        t("pay.pay_with_wallet", EN) in (text or "") for text, _ in _buttons(bot.keyboards[-1])
    ) or any("pay_wallet" in data for _, data in _buttons(bot.keyboards[-1]))


async def test_a_short_wallet_keeps_the_wallet_row_with_a_topup_link() -> None:
    """The wallet tap is always shown: a short wallet keeps it and gains top-up.

    Hiding the row left a buyer with no way forward; the deficit line on the
    card names what is missing and the top-up shortcut fixes it in one tap.
    """
    bot = RecordingBot()

    await _select_plan(bot, _CheckoutPool(balance=1_000))

    offered = [data for kb in bot.keyboards for _, data in _buttons(kb)]
    assert "pay_wallet:1" in offered, "the wallet tap stays visible when short"
    assert "menu:topup" in offered, "a short wallet needs the top-up shortcut"
    assert any("49,000" in text for text in bot.texts), "the card names the deficit"


async def test_paying_with_the_wallet_debits_and_grants_the_plan() -> None:
    bot = RecordingBot()
    pool = _CheckoutPool(balance=80_000)

    await payment_module.on_pay_with_wallet(
        _tap(bot, "pay_wallet:1"),
        cast(Any, pool),
        cast(Any, _buyer()),
        lang=EN,
    )

    assert pool.balances[USER_ID] == 30_000, "the plan price leaves the wallet"
    assert pool.granted == [(USER_ID, 30)], "the plan's days land on the account"
    expected = t(
        "pay.wallet_paid", EN, price="50,000", balance="30,000", currency="Toman"
    )
    assert expected in bot.texts


async def test_paying_with_a_short_wallet_grants_nothing() -> None:
    bot = RecordingBot()
    pool = _CheckoutPool(balance=1_000)

    await payment_module.on_pay_with_wallet(
        _tap(bot, "pay_wallet:1"),
        cast(Any, pool),
        cast(Any, _buyer()),
        lang=EN,
    )

    assert pool.balances[USER_ID] == 1_000
    assert pool.granted == []
