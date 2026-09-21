"""Manual (card-to-card) payment strategy.

Flow: user starts a purchase → a pending transaction is created → the user
sends a receipt photo → it is forwarded to admins with Approve/Reject inline
buttons → the admin's decision finalizes the transaction and grants premium.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

import asyncpg

from core import database
from services.payments.base import PaymentStrategy


class ManualPaymentStrategy(PaymentStrategy):
    """Receipt-photo flow with admin approval."""

    method = "manual"

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def begin(self, user: asyncpg.Record, plan: asyncpg.Record) -> asyncpg.Record:
        return await database.create_transaction(
            self._pool,
            telegram_id=user["telegram_id"],
            plan_id=plan["id"],
            amount=plan["price"],
            method=self.method,
        )

    async def attach_receipt(self, txn_id: uuid.UUID, photo_file_id: str) -> bool:
        return await database.attach_receipt(self._pool, txn_id, photo_file_id)

    async def decide(self, txn_id: uuid.UUID, approved: bool) -> Optional[dict[str, Any]]:
        return await database.decide_transaction(self._pool, txn_id, approved)
