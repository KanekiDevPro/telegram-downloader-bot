"""Payment strategy contracts (Strategy pattern).

Add a new payment method (e.g. a gateway) by subclassing ``PaymentStrategy``,
registering it in ``build_payment_service``, and using its ``method`` key in
the UI. The rest of the bot only talks to ``PaymentService``.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

import asyncpg


class PaymentStrategy(ABC):
    """One implementation per payment method (manual, gateway, ...)."""

    #: Unique method key stored in ``transactions.method``.
    method: str

    @abstractmethod
    async def begin(self, user: asyncpg.Record, plan: asyncpg.Record) -> asyncpg.Record:
        """Open a pending transaction for ``user`` + ``plan`` and return it."""

    @abstractmethod
    async def attach_receipt(self, txn_id: uuid.UUID, photo_file_id: str) -> bool:
        """Attach proof (receipt) to a transaction; False if it isn't pending."""

    @abstractmethod
    async def decide(self, txn_id: uuid.UUID, approved: bool) -> Optional[dict[str, Any]]:
        """Finalize a pending transaction.

        Returns an outcome dict (status, telegram_id, amount, duration_days)
        or None when the transaction doesn't exist / was already processed.
        """


class PaymentService:
    """Registry + facade over the available payment strategies."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._strategies: dict[str, PaymentStrategy] = {}

    def register(self, strategy: PaymentStrategy) -> None:
        self._strategies[strategy.method] = strategy

    def get(self, method: str) -> PaymentStrategy:
        try:
            return self._strategies[method]
        except KeyError:
            raise ValueError(f"no payment strategy registered for method {method!r}") from None
