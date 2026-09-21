"""Subscription & payment handlers: manual (card-to-card) flow + admin approvals.

Flow: /subscribe → pick a plan → strategy opens a pending transaction → user
sends a receipt photo → it's forwarded to every admin with Approve/Reject
inline buttons → the admin's decision grants (or declines) premium.
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
from core.utils import escape_html
from services.payments.base import PaymentService
from services.payments.manual import ManualPaymentStrategy

logger = logging.getLogger(__name__)
router = Router(name="payment")


class PaymentStates(StatesGroup):
    waiting_receipt = State()


def callback_message(cb: CallbackQuery) -> Message | None:
    """The message a callback is bound to, or None when it is inaccessible.

    Telegram may replace the message a callback was sent from with an
    ``InaccessibleMessage``; answering such a callback with a new message is
    impossible, so callers must handle ``None``.
    """
    return cb.message if isinstance(cb.message, Message) else None


#: The button that returns to the main menu. Owned here (not in ``user.py``)
#: because the plans screen is where a user most often wants to walk back.
BACK_BUTTON = ("🔙 بازگشت", "menu:home")


async def plans_keyboard(
    pool: asyncpg.Pool, *, back: bool = False
) -> InlineKeyboardMarkup | None:
    """The plan buttons, plus an optional back button. ``None`` = nothing for sale."""
    plans = await database.list_plans(pool)
    if not plans:
        return None
    builder = InlineKeyboardBuilder()
    for plan in plans:
        builder.button(
            text=f"{plan['name']} — {int(plan['price']):,} تومان",
            callback_data=f"plan:{plan['id']}",
        )
    if back:
        builder.button(text=BACK_BUTTON[0], callback_data=BACK_BUTTON[1])
    builder.adjust(1)
    return builder.as_markup()


async def send_plans(message: Message, pool: asyncpg.Pool, *, back: bool = False) -> None:
    """Show available subscription plans as inline buttons."""
    keyboard = await plans_keyboard(pool, back=back)
    if keyboard is None:
        await message.answer(NO_PLANS_TEXT)
        return
    await message.answer("یکی از پلن‌های اشتراک رو انتخاب کن 👇", reply_markup=keyboard)


#: Shown when the operator has not seeded any plan (or removed them all).
NO_PLANS_TEXT = "فعلاً پلنی برای فروش نداریم."


def _approval_keyboard(txn_id: uuid.UUID) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ تأیید", callback_data=f"txn_approve:{txn_id}")
    builder.button(text="❌ رد", callback_data=f"txn_reject:{txn_id}")
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


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message, pool: asyncpg.Pool) -> None:
    await send_plans(message, pool)


@router.callback_query(F.data.startswith("plan:"))
async def on_plan_selected(
    cb: CallbackQuery,
    state: FSMContext,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    user: asyncpg.Record,
) -> None:
    message = callback_message(cb)
    if message is None:
        await cb.answer("⚠️ این پیام دیگه در دسترس نیست؛ /subscribe رو دوباره بزن.", show_alert=True)
        return

    plan_id = _parse_plan_id(cb)
    if plan_id is None:
        await cb.answer("پلن نامعتبر.", show_alert=True)
        return
    plan = await database.get_plan(pool, plan_id)
    if plan is None:
        await cb.answer("پلن پیدا نشد.", show_alert=True)
        return

    strategy = payment_service.get(ManualPaymentStrategy.method)
    txn = await strategy.begin(user, plan)

    await state.set_state(PaymentStates.waiting_receipt)
    await state.update_data(txn_id=str(txn["id"]))

    settings = get_settings()
    await message.answer(
        "💳 <b>پرداخت کارت به کارت</b>\n\n"
        f"پلن: <b>{escape_html(plan['name'])}</b>\n"
        f"مبلغ: <b>{int(plan['price']):,} تومان</b>\n\n"
        f"به نام: {escape_html(settings.manual_card_holder or '—')}\n"
        f"شماره کارت: <code>{escape_html(settings.manual_card_number or '—')}</code>\n\n"
        "بعد از واریز، <b>عکس رسید</b> رو همین‌جا بفرست.\n"
        "برای انصراف: /cancel",
    )
    await cb.answer()


@router.message(PaymentStates.waiting_receipt, F.photo)
async def on_receipt_photo(
    message: Message,
    state: FSMContext,
    pool: asyncpg.Pool,
    payment_service: PaymentService,
    bot: Bot,
) -> None:
    photos = message.photo
    from_user = message.from_user
    if not photos or from_user is None:  # F.photo already guarantees both
        return

    data = await state.get_data()
    try:
        txn_id = uuid.UUID(data.get("txn_id", ""))
    except (ValueError, TypeError):
        await message.answer("تراکنش نامعتبر است؛ /subscribe بزن و دوباره تلاش کن.")
        return

    photo_file_id = photos[-1].file_id
    strategy = payment_service.get(ManualPaymentStrategy.method)
    attached = await strategy.attach_receipt(txn_id, photo_file_id)
    if not attached:
        await message.answer("این رسید به تراکنش فعالی تعلق نداره؛ /subscribe بزن و دوباره امتحان کن.")
        return
    await state.clear()

    txn = await database.get_transaction(pool, txn_id)
    if txn is None:  # deleted between attach and read — don't explode in the admin forward
        logger.warning("receipt attached but transaction %s is gone", txn_id)
        await message.answer("این تراکنش پیدا نشد؛ /subscribe بزن و دوباره تلاش کن.")
        return
    plan = await database.get_plan(pool, txn["plan_id"])
    settings = get_settings()
    user_name = f"@{from_user.username}" if from_user.username else str(from_user.id)
    caption = (
        "🧾 <b>رسید پرداخت جدید</b>\n\n"
        f"کاربر: {escape_html(user_name)} (ID: <code>{from_user.id}</code>)\n"
        f"پلن: {escape_html(plan['name']) if plan else '?'}\n"
        f"مبلغ: {int(txn['amount']):,} تومان\n"
        f"شناسه تراکنش: <code>{txn_id}</code>\n"
        f"روش: کارت به کارت"
    )

    notified = 0
    for admin_id in settings.admin_ids:
        try:
            await bot.send_photo(
                admin_id,
                photo_file_id,
                caption=caption,
                reply_markup=_approval_keyboard(txn_id),
            )
            notified += 1
        except Exception:
            logger.exception("failed to forward receipt to admin %s", admin_id)
    if notified == 0:
        logger.warning("receipt accepted but no admin notified — check ADMIN_IDS in .env")

    await message.answer("✅ رسید دریافت شد. پس از تأیید ادمین، اشتراکت فعال می‌شه.")


async def _admin_decide(
    cb: CallbackQuery, bot: Bot, payment_service: PaymentService, *, approved: bool
) -> None:
    settings = get_settings()
    if not settings.is_admin(cb.from_user.id):
        await cb.answer("⛔️ فقط ادمین می‌تونه.", show_alert=True)
        return

    txn_id = _parse_txn_id(cb)
    if txn_id is None:
        await cb.answer("تراکنش نامعتبر.", show_alert=True)
        return

    strategy = payment_service.get(ManualPaymentStrategy.method)
    outcome = await strategy.decide(txn_id, approved)
    if outcome is None:
        await cb.answer("این تراکنش قبلاً پردازش شده.", show_alert=True)
        return

    status_fa = "✅ تأیید شد" if approved else "❌ رد شد"
    await cb.answer(status_fa)

    message = callback_message(cb)
    caption = message.caption if message is not None else None
    if message is not None and caption is not None:
        try:
            await message.edit_caption(
                caption=f"{caption}\n\n{status_fa} توسط {escape_html(cb.from_user.full_name)}"
            )
        except Exception:
            logger.debug("could not edit admin caption", exc_info=True)

    try:
        await bot.send_message(
            outcome["telegram_id"],
            "🎉 پرداختت تأیید شد! اشتراک پریمیوم فعال شد — ممنون از همراهی‌ات 🚀"
            if approved
            else "❌ پرداختت رد شد. اگه فکر می‌کنی اشتباهه با پشتیبانی در ارتباط باش.",
        )
    except Exception:
        logger.exception("could not notify user %s", outcome["telegram_id"])


@router.callback_query(F.data.startswith("txn_approve:"))
async def on_admin_approve(cb: CallbackQuery, bot: Bot, payment_service: PaymentService) -> None:
    await _admin_decide(cb, bot, payment_service, approved=True)


@router.callback_query(F.data.startswith("txn_reject:"))
async def on_admin_reject(cb: CallbackQuery, bot: Bot, payment_service: PaymentService) -> None:
    await _admin_decide(cb, bot, payment_service, approved=False)
