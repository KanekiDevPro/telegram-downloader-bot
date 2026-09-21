"""Subscription / entitlement helpers (premium status, effective daily limits)."""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg

from core.config import get_settings


def is_premium_active(user: asyncpg.Record) -> bool:
    """Premium flag that also respects the expiry timestamp."""
    if not user["is_premium"]:
        return False
    until = user["premium_until"]
    return until is None or until > datetime.now(timezone.utc)


def effective_daily_limit(user: asyncpg.Record) -> int:
    """Daily download quota for a user (premium gets the higher tier)."""
    settings = get_settings()
    return settings.premium_daily_limit if is_premium_active(user) else settings.default_daily_limit
