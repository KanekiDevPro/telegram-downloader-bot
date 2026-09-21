"""Payment strategies package — Strategy pattern, one class per payment method."""

from __future__ import annotations

import asyncpg

from core.config import get_settings  # noqa: F401  (kept for future strategy wiring)
from services.payments.base import PaymentService, PaymentStrategy
from services.payments.manual import ManualPaymentStrategy

__all__ = ["PaymentService", "PaymentStrategy", "ManualPaymentStrategy", "build_payment_service"]


def build_payment_service(pool: asyncpg.Pool) -> PaymentService:
    """Create the payment service and register every available strategy."""
    service = PaymentService(pool)
    service.register(ManualPaymentStrategy(pool))
    return service
