"""Subscription & payment handlers: manual (card-to-card) flow + admin approvals.

Flow: /subscribe → pick a plan → strategy opens a pending transaction → user
sends a receipt photo → it's forwarded to every admin with Approve/Reject
inline buttons → the admin's decision grants (or declines) premium.

Everything a user reads comes from the catalogue in their language, and one line
deliberately does not: the decision is announced to the *buyer* in the language
stored on their account (not the admin's), because the person reading it is the
person who paid — the admin who tapped the button knows what they tapped.
"""

from __future__ import annotations

import logging
import uuid

import asyncpg
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import database
from core.config import get_settings
from core.i18n import DEFAULT_LANG, plan_name, t
from core.ui import callback_message as callback_message
from core.utils import escape_html
from services import recipients
from services.payments.base import PaymentService
from services.payments.manual import ManualPaymentStrategy

logger = logging.getLogger(__name__)
router = Router(name="payment")


class PaymentStates(StatesGroup):
    waiting_receipt = State()





async def plans_keyboard(
    pool: asyncpg.Pool, *, lang: str = DEFAULT_LANG, back: bool = False
) -> InlineKeyboardMarkup | None:
    """The plan buttons, plus an optional back button. ``None`` = nothing for sale."""
    plans = await database.list_plans(pool)
    if not plans:
        return None
    builder = InlineKeyboardBuilder()
    for plan in plans:
        builder.button(
            text=t(
                "pay.plan_button",
                lang,
                name=plan_name(plan["name"], lang),
                price=f"{int(plan['price']):,}",
                currency=t("pay.currency", lang),
            ),
            callback_data=f"plan:{plan['id']}",
        )
    if back:
        builder.button(text=t("menu.back", lang), callback_data="menu:home")
    builder.adjust(1)
    return builder.as_markup()


async def send_plans(
    message: Message, pool: asyncpg.Pool, *, lang: str = DEFAULT_LANG, back: bool = False
) -> None:
    """Show available subscription plans as inline buttons."""
    keyboard = await plans_keyboard(pool, lang=lang, back=back)
    if keyboard is None:
        await message.answer(t("pay.no_plans", lang))
        return
    await message.answer(t("pay.pick_plan", lang), reply_markup=keyboard)


def _approval_keyboard(txn_id: uuid.UUID, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("pay.approve", lang), callback_data=f"txn_approve:{txn_id}")
    builder.button(text=t("pay.reject", lang), callback_data=f"txn_reject:{txn_id}")
    builder.adjust(2)
    return builder.as_markup()


def _parse_plan_id(cb: CallbackQuery) -> int | None:
    try:
        return int((cb.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        return None


def _parse_txn_id(cb: CallbackQuery) -> uuid.UUID | None:
    try:
        return uuid.UUID((cb.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        return None


async def _pending_transaction(pool: asyncpg.Pool, state: FSMContext) -> asyncpg.Record | None:
    """The transaction the buyer's FSM still points at, if it is still open.

    Plan selection is idempotent through this: a second tap must re-send the
    card the buyer already has, never mint a second pending transaction for
    the same purchase (two cards, one receipt, one orphaned row).
    """
    data = await state.get_data()
    try:
        txn_id = uuid.UUID(str(data.get("txn_id", "")))
    except (ValueError, TypeError):
        return None
    txn = await database.get_transaction(pool, txn_id)
    return txn if txn is not None and txn["status"] == "pending" else None


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, pool: asyncpg.Pool, lang: str = DEFAULT_LANG) -> None:
    await send_plans(message, pool, lang=lang)


@router.callback_query(F.data.startswith("plan:"))
async def on_plan_selected(
    cb: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    user: asyncpg.Record,
    lang: str = DEFAULT_LANG,
) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("pay.stale", lang), show_alert=True)
        return

    plan_id = _parse_plan_id(cb)
    if plan_id is None:
        await cb.answer(t("pay.plan_invalid", lang), show_alert=True)
        return
    plan = await database.get_plan(pool, plan_id)
    if plan is None:
        await cb.answer(t("pay.plan_missing", lang), show_alert=True)
        return

    txn = await _pending_transaction(pool, state)
    if txn is None:
        strategy = payment_service.get(ManualPaymentStrategy.method)
        txn = await strategy.begin(user, plan)
        await state.set_state(PaymentStates.waiting_receipt)
        await state.update_data(txn_id=str(txn["id"]))
    else:
        # The buyer already has this purchase open: re-send its card, not a
        # second transaction the receipt could then orphan.
        plan_row = await database.get_plan(pool, txn["plan_id"])
        if plan_row is not None:
            plan = plan_row

    settings = get_settings()
    price = int(txn["amount"])
    keyboard = await _pay_options_keyboard(pool, user, plan, price, lang)
    await message.answer(
        t(
            "pay.card_title",
            lang,
            plan=escape_html(plan_name(plan["name"], lang)),
            price=f"{price:,}",
            currency=t("pay.currency", lang),
            holder=escape_html(settings.manual_card_holder or "—"),
            card=escape_html(settings.manual_card_number or "—"),
        ),
        reply_markup=keyboard,
    )
    await cb.answer()


async def _pay_options_keyboard(
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    plan: asyncpg.Record,
    price: int,
    lang: str,
) -> InlineKeyboardMarkup | None:
    """The card's keyboard: the wallet tap, when the wallet covers the plan.

    The wallet is an *explicit* option, never a default: a buyer who would
    rather keep the credit still has the receipt route (the card text), and a
    short wallet is simply not offered a tap that could only refuse it.
    """
    balance = await database.get_wallet_balance(pool, user["telegram_id"])
    if balance < price:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t("pay.pay_with_wallet", lang),
        callback_data=f"pay_wallet:{plan['id']}",
    )
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("pay_wallet:"))
async def on_pay_with_wallet(
    cb: CallbackQuery,
    pool: asyncpg.Pool,
    user: asyncpg.Record,
    lang: str = DEFAULT_LANG,
) -> None:
    """Buy the plan with the internal wallet: one guarded debit, then the grant.

    The debit is atomic (``debit_wallet`` refuses a short wallet itself), so a
    balance that shrank between the card and the tap lands on the honest refusal
    instead of a negative wallet — and a debited wallet is always followed by
    its grant on this same path.
    """
    message = callback_message(cb)
    if message is None:
        await cb.answer(t("pay.stale", lang), show_alert=True)
        return
    try:
        plan_id = int((cb.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer(t("pay.plan_invalid", lang), show_alert=True)
        return
    plan = await database.get_plan(pool, plan_id)
    if plan is None:
        await cb.answer(t("pay.plan_missing", lang), show_alert=True)
        return
    price = int(plan["price"])
    telegram_id = user["telegram_id"]
    new_balance = await database.debit_wallet(pool, telegram_id, price)
    if new_balance is None:
        balance = await database.get_wallet_balance(pool, telegram_id)
        await cb.answer(
            t(
                "pay.wallet_short",
                lang,
                balance=f"{balance:,}",
                price=f"{price:,}",
                currency=t("pay.currency", lang),
            ),
            show_alert=True,
        )
        return
    await database.grant_premium(pool, telegram_id, int(plan["duration_days"]))
    await cb.answer()
    await message.answer(
        t(
            "pay.wallet_paid",
            lang,
            price=f"{price:,}",
            balance=f"{new_balance:,}",
            currency=t("pay.currency", lang),
        )
    )


@router.message(PaymentStates.waiting_receipt, F.photo)
async def on_receipt_photo(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    bot: Bot,
    lang: str = DEFAULT_LANG,
) -> None:
    photos = message.photo
    from_user = message.from_user
    if not photos or from_user is None:  # F.photo already guarantees both
        return

    data = await state.get_data()
    try:
        txn_id = uuid.UUID(data.get("txn_id", ""))
    except (ValueError, TypeError):
        await message.answer(t("pay.txn_invalid", lang))
        return

    photo_file_id = photos[-1].file_id
    strategy = payment_service.get(ManualPaymentStrategy.method)
    attached = await strategy.attach_receipt(txn_id, photo_file_id, from_user.id)
    if not attached:
        await message.answer(t("pay.txn_missing", lang))
        return
    await state.clear()

    txn = await database.get_transaction(pool, txn_id)
    if txn is None:  # deleted between attach and read — don't explode in the admin forward
        logger.warning("receipt attached but transaction %s is gone", txn_id)
        await message.answer(t("pay.txn_gone", lang))
        return
    plan = await database.get_plan(pool, txn["plan_id"])
    settings = get_settings()
    user_name = f"@{from_user.username}" if from_user.username else str(from_user.id)

    notified = 0
    # Each admin reads the receipt in their own language: the caption is data (who,
    # what, how much), and only its labels are translated. One query for the list.
    for admin_id, admin_lang in await recipients.targets(
        pool, settings.admin_ids, fallback=lang
    ):
        caption = t(
            "pay.receipt_caption",
            admin_lang,
            user=escape_html(user_name),
            telegram_id=from_user.id,
            plan=escape_html(plan_name(plan["name"], admin_lang)) if plan else "?",
            price=f"{int(txn['amount']):,}",
            currency=t("pay.currency", admin_lang),
            txn_id=txn_id,
        )
        try:
            await bot.send_photo(
                admin_id,
                photo_file_id,
                caption=caption,
                reply_markup=_approval_keyboard(txn_id, admin_lang),
            )
            notified += 1
        except Exception:
            logger.exception("failed to forward receipt to admin %s", admin_id)
    if notified == 0:
        logger.warning("receipt accepted but no admin notified — check ADMIN_IDS in .env")
        # The receipt is stored and the transaction stays pending; being
        # thanked here would bury a payment no admin will ever decide.
        await message.answer(t("pay.receipt_unreachable", lang))
        return

    await message.answer(t("pay.receipt_received", lang))


async def _buyer_language(pool: asyncpg.Pool, telegram_id: int, *, fallback: str) -> str:
    """The language the *buyer* reads — their own, not the approving admin's."""
    found = await recipients.targets(pool, [telegram_id], fallback=fallback)
    return found[0][1] if found else fallback


async def _admin_decide(
    cb: CallbackQuery,
    bot: Bot,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    *,
    approved: bool,
    lang: str = DEFAULT_LANG,
) -> None:
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        await cb.answer(t("pay.admin_only", lang), show_alert=True)
        return

    txn_id = _parse_txn_id(cb)
    if txn_id is None:
        await cb.answer(t("pay.txn_invalid", lang), show_alert=True)
        return

    strategy = payment_service.get(ManualPaymentStrategy.method)
    outcome = await strategy.decide(txn_id, approved)
    if outcome is None:
        await cb.answer(t("pay.already_decided", lang), show_alert=True)
        return

    status = t("pay.approved_note" if approved else "pay.rejected_note", lang)
    await cb.answer(status)

    message = callback_message(cb)
    caption = message.caption if message is not None else None
    if message is not None and caption is not None:
        try:
            decided = t(
                "pay.decided_by",
                lang,
                status=status,
                admin=escape_html(cb.from_user.full_name),
            )
            await message.edit_caption(
                caption=f"{caption}\n\n{decided}",
                # Omitting reply_markup would keep the live Approve/Reject
                # buttons on a transaction this verdict just closed.
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            )
        except Exception:
            logger.debug("could not edit admin caption", exc_info=True)

    # The *buyer's* language, not the admin's: this line lands in the buyer's chat.
    buyer_lang = await _buyer_language(pool, int(outcome["telegram_id"]), fallback=lang)
    try:
        await bot.send_message(
            outcome["telegram_id"],
            t("pay.granted_user" if approved else "pay.declined_user", buyer_lang),
        )
    except Exception:
        logger.exception("could not notify user %s", outcome["telegram_id"])


@router.callback_query(F.data.startswith("txn_approve:"))
async def on_admin_approve(
    cb: CallbackQuery,
    bot: Bot,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    lang: str = DEFAULT_LANG,
) -> None:
    await _admin_decide(cb, bot, pool, payment_service, approved=True, lang=lang)


@router.callback_query(F.data.startswith("txn_reject:"))
async def on_admin_reject(
    cb: CallbackQuery,
    bot: Bot,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    lang: str = DEFAULT_LANG,
) -> None:
    await _admin_decide(cb, bot, pool, payment_service, approved=False, lang=lang)
