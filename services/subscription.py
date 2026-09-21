"""Subscription / entitlement helpers (premium status, effective daily limits).

Admins are not a special case of premium, they are a third state: they never pay,
never expire, and are never stopped by a quota. That state is decided from
``ADMIN_IDS`` on every call rather than written into the database, for two reasons —
an operator who adds their own id to ``.env`` should not have to also remember a
subscription record, and an id removed from ``.env`` must lose the privileges of the
bot on the next check rather than at the end of a billing period.

The quota is a sentinel and not "infinity": the worker counts the download anyway
(one integer, and the profile shows the real number), and only the *ceiling* is out
of reach. Nothing in the download path needs to know about admins.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg

from core.config import get_settings

#: The daily ceiling an admin is measured against: far past any real use, so no
#: check has to be skipped and no counter has to be special-cased. The profile says
#: «unlimited ♾» instead of printing it.
UNLIMITED_DAILY_LIMIT = 10**7


def is_admin_id(telegram_id: int | None) -> bool:
    """Whether this Telegram id is in ``ADMIN_IDS``."""
    return get_settings().is_admin(telegram_id)


def is_admin(user: asyncpg.Record) -> bool:
    """Whether this user record belongs to an admin."""
    try:
        return is_admin_id(user["telegram_id"])
    except (KeyError, IndexError, TypeError):  # a stub/record without the column
        return False


def is_premium_active(user: asyncpg.Record) -> bool:
    """Premium flag that also respects the expiry timestamp — admins always count.

    A payment check that an admin can fail is a payment check an operator has to
    work around, which is how "temporarily" becomes a hard-coded exception somewhere
    else.
    """
    if is_admin(user):
        return True
    if not user["is_premium"]:
        return False
    until = user["premium_until"]
    return until is None or until > datetime.now(timezone.utc)


def effective_daily_limit(user: asyncpg.Record) -> int:
    """Daily download quota for a user (premium gets the higher tier)."""
    if is_admin(user):
        return UNLIMITED_DAILY_LIMIT
    settings = get_settings()
    return settings.premium_daily_limit if is_premium_active(user) else settings.default_daily_limit
