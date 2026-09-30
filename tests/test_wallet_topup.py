"""Full wallet top-up flow: amounts, card + tracking ref, receipt queue, VIP wallet row.

A top-up is real money moved by hand: the user picks an amount (or types one),
reads the official card plus a unique tracking reference, and sends a receipt
photo while in a waiting state. An admin decides in a review queue — approve
credits the wallet atomically via ``add_wallet_credit`` and notifies the user,
reject notifies without touching the balance. And the VIP checkout always shows
the pay-with-wallet row: one tap when funded, the deficit plus a top-up link
when short.
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
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, User

from core.config import Settings
from core.i18n import t
from handlers import payment as payment_module
from handlers import user as user_module

EN = "en"
FA = "fa"
USER_ID = 4242
ADMIN_ID = 777
USERNAME = "buyer"


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text or "", button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


class RecordingBot:
    """Captures every screen (sends and in-place edits) plus admin forwards."""

    def __init__(self, *, deliverable: bool = True) -> None:
        self.deliverable = deliverable
        self.texts: list[str] = []
        self.keyboards: list[Any] = []
        self.forwards: list[int] = []
        self.sent_to_user: list[tuple[int, str]] = []

    async def __call__(self, method: Any) -> Any:
        if isinstance(method, (SendMessage, EditMessageText)):
            self.texts.append(method.text or "")
            if method.reply_markup is not None:
                self.keyboards.append(method.reply_markup)
        return SimpleNamespace(message_id=11)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent_to_user.append((chat_id, text))
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
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer", username=USERNAME),
    ).as_(cast(Bot, bot))


def _photo_message(bot: RecordingBot) -> Message:
    return Message(
        message_id=10,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type=cast(Any, "private")),
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer", username=USERNAME),
        photo=[PhotoSize(file_id="topup-receipt", file_unique_id="uniq", width=1, height=1)],
    ).as_(cast(Bot, bot))


def _tap(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer", username=USERNAME),
        chat_instance="chat",
        data=data,
        message=_message(bot),
    ).as_(cast(Bot, bot))


def _admin_tap(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="2",
        from_user=User(id=ADMIN_ID, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=data,
        message=_message(bot),
    ).as_(cast(Bot, bot))


def _fsm(bot_id: int = 1) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot_id, chat_id=USER_ID, user_id=USER_ID),
    )


@pytest.fixture
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_wallet_balance(pool: Any, telegram_id: int) -> int:
        return 20_000

    async def get_button_looks(pool: Any) -> dict[str, Any]:
        return {}

    def clean_settings() -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            MANUAL_CARD_NUMBER="6037-9911-1234-5678",
            MANUAL_CARD_HOLDER="Bot Owner",
            ADMIN_IDS=str(ADMIN_ID),  # type: ignore[arg-type]
        )

    async def targets(pool: Any, ids: Any, *, fallback: str = "") -> list[tuple[int, str]]:
        return [(ADMIN_ID, EN)]

    monkeypatch.setattr(user_module.database, "get_wallet_balance", get_wallet_balance)
    monkeypatch.setattr(user_module.database, "get_button_looks", get_button_looks)
    monkeypatch.setattr(payment_module, "get_settings", clean_settings)
    monkeypatch.setattr(payment_module.recipients, "targets", targets)


# ---------------------------------------------------------------------------
# 1. Amount selection: predefined buttons + custom
# ---------------------------------------------------------------------------


async def test_topup_screen_offers_predefined_amounts_and_custom(_clean_env: None) -> None:
    """Tapping top-up must offer the amount grid, not a bare balance line."""
    bot = RecordingBot()
    user = cast(Any, {"telegram_id": USER_ID, "username": USERNAME, "language": FA})

    await user_module.on_menu_topup(_tap(bot, "menu:topup"), user, cast(Any, object()), lang=FA)

    assert bot.keyboards, "the top-up screen must carry amount buttons"
    offered = [data for kb in bot.keyboards for _, data in _buttons(kb)]
    assert any(data.startswith("topup:") and data != "topup:custom" for data in offered), (
        f"predefined amount buttons missing: {offered!r}"
    )
    assert "topup:custom" in offered, f"the custom-amount option is missing: {offered!r}"


async def test_selecting_an_amount_shows_card_holder_and_tracking_ref(_clean_env: None) -> None:
    """The card screen names the official card, the amount and a unique ref."""
    bot = RecordingBot()
    state = _fsm()

    await payment_module.on_topup_amount(_tap(bot, "topup:100000"), state, lang=EN)

    assert bot.texts, "choosing an amount must show the payment card"
    card = bot.texts[-1]
    assert "6037" in card or "Card number" in card or "شماره کارت" in card, (
        f"the card number is missing from: {card!r}"
    )
    data = await state.get_data()
    assert int(data.get("wallet_amount", 0)) == 100_000
    ref = str(data.get("wallet_ref", ""))
    assert len(ref) >= 6, f"a unique tracking reference is required, got {ref!r}"
    assert ref in card, "the tracking reference must be shown with the card"


async def test_each_amount_choice_mints_a_fresh_tracking_ref(_clean_env: None) -> None:
    bot = RecordingBot()

    await payment_module.on_topup_amount(_tap(bot, "topup:50000"), _fsm(), lang=EN)
    first = bot.texts[-1]
    await payment_module.on_topup_amount(_tap(bot, "topup:50000"), _fsm(), lang=EN)
    second = bot.texts[-1]

    assert first != second, "two identical amounts must still get distinct tracking refs"


def _text_message(bot: RecordingBot, text: str) -> Message:
    return Message(
        message_id=11,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type=cast(Any, "private")),
        from_user=User(id=USER_ID, is_bot=False, first_name="buyer", username=USERNAME),
        text=text,
    ).as_(cast(Bot, bot))


async def test_custom_amount_prompts_then_shows_the_card(_clean_env: None) -> None:
    """``topup:custom`` asks for a number; a valid reply shows the same card."""
    bot = RecordingBot()
    state = _fsm()

    await payment_module.on_topup_amount(_tap(bot, "topup:custom"), state, lang=EN)
    assert bot.texts, "the custom option must prompt for an amount"

    await payment_module.on_topup_custom_amount(_text_message(bot, "100000"), state, lang=EN)

    card = bot.texts[-1]
    data = await state.get_data()
    assert int(data.get("wallet_amount", 0)) == 100_000
    assert str(data.get("wallet_ref", "")) in card


async def test_custom_amount_rejects_garbage_without_leaving_the_prompt(_clean_env: None) -> None:
    bot = RecordingBot()
    state = _fsm()

    await payment_module.on_topup_amount(_tap(bot, "topup:custom"), state, lang=EN)
    before = len(bot.texts)
    await payment_module.on_topup_custom_amount(_text_message(bot, "not-a-number"), state, lang=EN)

    assert len(bot.texts) > before, "garbage input must be answered with a retry prompt"
    assert not (await state.get_data()).get("wallet_ref"), "no card is minted for garbage"


# ---------------------------------------------------------------------------
# 2. Receipt state + admin approval queue
# ---------------------------------------------------------------------------


async def test_wallet_receipt_forwards_user_amount_and_ref_to_admins(_clean_env: None) -> None:
    """The receipt photo reaches the admin queue with who paid what (and ref)."""
    bot = RecordingBot()
    state = _fsm()
    await state.set_state(payment_module.WalletStates.waiting_receipt)
    await state.update_data(wallet_amount=100_000, wallet_ref="AB12CD34")

    await payment_module.on_wallet_receipt(
        _photo_message(bot), state, cast(Any, object()), cast(Bot, bot), lang=EN
    )

    assert bot.forwards == [ADMIN_ID], "the receipt must reach the admin review queue"
    assert t("pay.receipt_received", EN) in bot.texts
    state_now = await state.get_state()
    assert state_now is None, "the receipt ends the waiting state"


async def test_admin_approve_credits_the_wallet_and_notifies_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approve atomically credits via ``add_wallet_credit`` and confirms."""
    bot = RecordingBot()
    credited: list[tuple[int, int]] = []

    async def add_wallet_credit(pool: Any, telegram_id: int, amount: int) -> int:
        credited.append((telegram_id, amount))
        return 120_000

    def admin_settings() -> Settings:
        return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(payment_module.database, "add_wallet_credit", add_wallet_credit)
    monkeypatch.setattr(payment_module, "get_settings", admin_settings)

    await payment_module.on_wallet_approve(
        _admin_tap(bot, f"wallet_approve:{USER_ID}:100000:AB12CD34"),
        cast(Bot, bot),
        cast(Any, object()),
        lang=EN,
    )

    assert credited == [(USER_ID, 100_000)], "approve must credit exactly the receipt amount"
    assert bot.sent_to_user, "the user must be notified of the credit"
    notified = " ".join(text for _, text in bot.sent_to_user)
    assert "100" in notified and "120" in notified, f"confirmation must name amount+balance: {notified!r}"


async def test_admin_reject_notifies_without_crediting(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = RecordingBot()
    credited: list[tuple[int, int]] = []

    async def add_wallet_credit(pool: Any, telegram_id: int, amount: int) -> int:
        credited.append((telegram_id, amount))
        return 0

    def admin_settings() -> Settings:
        return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(payment_module.database, "add_wallet_credit", add_wallet_credit)
    monkeypatch.setattr(payment_module, "get_settings", admin_settings)

    await payment_module.on_wallet_reject(
        _admin_tap(bot, f"wallet_reject:{USER_ID}:100000:AB12CD34"),
        cast(Bot, bot),
        cast(Any, object()),
        lang=EN,
    )

    assert credited == [], "a rejection must never touch the wallet"
    assert bot.sent_to_user, "the user must learn the request was rejected"


# ---------------------------------------------------------------------------
# 3. VIP checkout: wallet row always visible, deficit + top-up link when short
# ---------------------------------------------------------------------------


async def test_vip_checkout_always_shows_wallet_with_topup_link_when_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short wallet still sees the wallet row: deficit plus a top-up shortcut."""
    pool = cast(Any, object())
    user = cast(Any, {"telegram_id": USER_ID})
    plan = cast(Any, {"id": 1, "price": 50_000})

    async def short_balance(pool: Any, telegram_id: int) -> int:
        return 1_000

    monkeypatch.setattr(payment_module.database, "get_wallet_balance", short_balance)
    keyboard = await payment_module._pay_options_keyboard(pool, user, plan, 50_000, EN)

    assert keyboard is not None, "the wallet row must not vanish when the balance is short"
    offered = [data for _, data in _buttons(keyboard)]
    assert "pay_wallet:1" in offered, f"pay-with-wallet must stay visible: {offered!r}"
    assert "menu:topup" in offered, f"a short wallet needs the top-up link: {offered!r}"
