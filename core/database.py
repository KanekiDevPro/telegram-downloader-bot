"""PostgreSQL access: asyncpg connection pool, schema bootstrap, typed accessors."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

import asyncpg

from core.config import get_settings
from core.utils import utcnow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema (idempotent)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
DO $$ BEGIN
    CREATE TYPE transaction_status AS ENUM ('pending', 'approved', 'rejected');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE payment_method AS ENUM ('manual');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS users (
    telegram_id        BIGINT PRIMARY KEY,
    username           TEXT,
    -- 'en' or 'fa'. Seeded from the Telegram locale on first contact and only ever
    -- changed by the user (/language or the welcome buttons), because a background
    -- worker's progress message has no locale to look at.
    language           TEXT NOT NULL DEFAULT 'en',
    is_premium         BOOLEAN NOT NULL DEFAULT FALSE,
    premium_until      TIMESTAMPTZ,
    daily_downloads    INTEGER NOT NULL DEFAULT 0,
    last_download_date DATE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The language column arrived after the first release; existing rows read as
-- English, which is the product's default anyway.
ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'en';

CREATE TABLE IF NOT EXISTS subscription_plans (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    duration_days INTEGER NOT NULL,
    price         NUMERIC(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id               UUID PRIMARY KEY,
    telegram_id      BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    plan_id          INTEGER NOT NULL REFERENCES subscription_plans(id),
    amount           NUMERIC(10, 2) NOT NULL,
    status           transaction_status NOT NULL DEFAULT 'pending',
    method           payment_method NOT NULL DEFAULT 'manual',
    receipt_photo_id TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_transactions_telegram_id ON transactions (telegram_id);
CREATE INDEX IF NOT EXISTS ix_transactions_status     ON transactions (status);

CREATE TABLE IF NOT EXISTS smart_cache (
    url_hash         CHAR(64) PRIMARY KEY,
    original_url     TEXT NOT NULL,
    platform         TEXT NOT NULL,
    telegram_file_id TEXT NOT NULL,
    quality          TEXT NOT NULL DEFAULT 'best',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_smart_cache_platform ON smart_cache (platform);

-- How the stored ``telegram_file_id`` has to be re-sent: ``video`` / ``audio`` /
-- ``photo`` / ``photo_group`` / ``file``. Added after the first release, hence
-- nullable: rows written before this column existed carry NULL and are delivered
-- the old way (by ``quality``) instead of as documents. A ``photo_group`` row keeps
-- a JSON list of file_ids in ``telegram_file_id`` — one entry cannot describe an
-- album, and a schema just for that would be a table for a caption.
ALTER TABLE smart_cache ADD COLUMN IF NOT EXISTS kind TEXT;

-- Every failed download, with the cause we diagnosed. A block has a small set of
-- meanings (our login, the IP, the site itself, a stale session) and only the
-- first is fixable from here — so the counts, not the individual rows, are the
-- point: they turn "downloads sometimes fail" into a decision.
CREATE TABLE IF NOT EXISTS block_events (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- No FK on purpose: telemetry must survive a user row being deleted.
    telegram_id BIGINT,
    url_host    TEXT NOT NULL,
    code        TEXT NOT NULL,
    -- Text with a check rather than an enum: one ALTER adds a cause later, and
    -- each insert would otherwise need an explicit enum cast through asyncpg.
    cause       TEXT NOT NULL CHECK (cause IN ('login', 'ip', 'site', 'session'))
);

CREATE INDEX IF NOT EXISTS ix_block_events_created_at ON block_events (created_at);
CREATE INDEX IF NOT EXISTS ix_block_events_cause      ON block_events (cause);

-- Small key/value corner for "when did the bot last do X" (the weekly digest).
CREATE TABLE IF NOT EXISTS bot_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- What was *done* about the failures. The digest says which cause dominates; it
-- cannot say whether the fix worked, because nothing recorded when the fix
-- happened. This is that record: a replaced cookie jar (automatic or manual), or
-- a fresh export the running bot picked up. The trend view reads it to compare
-- the failures before and after each fix.
CREATE TABLE IF NOT EXISTS fix_events (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 'cookie_jar' for now; text so a later fix (proxy, PO token) is a new value,
    -- not a migration.
    kind       TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_fix_events_created_at ON fix_events (created_at);
CREATE INDEX IF NOT EXISTS ix_fix_events_kind      ON fix_events (kind);

-- The helper servers this stack runs for YouTube (a PO-token provider and a session
-- server). When one is down every link on that route fails the same way, and no
-- user complaint says which — so one row per observed *change* of state, with the
-- reason, makes "was the no-login route dead last week?" a query instead of a
-- memory. Transitions only: a helper that is simply off is one row, not a row per
-- check.
CREATE TABLE IF NOT EXISTS helper_events (
    id         BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 'pot' (the PO-token provider) or 'session' (the YouTube session server).
    helper     TEXT NOT NULL,
    -- 'off' | 'ok' | 'warming' | 'drift' | 'down'.
    state      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_helper_events_created_at ON helper_events (created_at);
CREATE INDEX IF NOT EXISTS ix_helper_events_helper     ON helper_events (helper);
"""

# Inserted on first bootstrap; edit freely afterwards in DB (seeding is one-shot).
SEED_PLANS: tuple[dict[str, object], ...] = (
    {"name": "۱ هفته", "duration_days": 7, "price": Decimal("99000.00")},
    {"name": "۱ ماه", "duration_days": 30, "price": Decimal("249000.00")},
    {"name": "۳ ماه", "duration_days": 90, "price": Decimal("649000.00")},
)


# ---------------------------------------------------------------------------
# Pool / bootstrap
# ---------------------------------------------------------------------------

async def create_pool() -> asyncpg.Pool:
    """Create an asyncpg connection pool from DATABASE_URL."""
    settings = get_settings()
    return await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=2,
        max_size=10,
        command_timeout=60,
    )


async def init_db(pool: asyncpg.Pool) -> None:
    """Create tables/enums and seed subscription plans. Safe to run repeatedly."""
    await pool.execute(SCHEMA_SQL)
    count = await pool.fetchval("SELECT COUNT(*) FROM subscription_plans")
    if count == 0:
        await pool.executemany(
            "INSERT INTO subscription_plans (name, duration_days, price) VALUES ($1, $2, $3)",
            [(p["name"], p["duration_days"], p["price"]) for p in SEED_PLANS],
        )


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

async def get_or_create_user(
    pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    language: str | None = None,
) -> asyncpg.Record:
    """Insert the user if new; refresh the username if it changed. Returns the row.

    ``language`` is only ever used on the *insert*: it is the Telegram locale of a
    first contact (so a Persian speaker starts in Persian), never a reason to
    overwrite an existing choice — a user who picked English must keep it even if
    their client is in Persian.
    """
    return await pool.fetchrow(
        """
        INSERT INTO users (telegram_id, username, language)
        VALUES ($1, $2, COALESCE($3, 'en'))
        ON CONFLICT (telegram_id) DO UPDATE
            SET username = COALESCE(EXCLUDED.username, users.username)
        RETURNING *
        """,
        telegram_id,
        username,
        language,
    )


async def set_user_language(pool: asyncpg.Pool, telegram_id: int, language: str) -> None:
    """Store a user's language choice."""
    await pool.execute(
        "UPDATE users SET language = $2, updated_at = now() WHERE telegram_id = $1",
        telegram_id,
        language,
    )


async def languages_for(pool: asyncpg.Pool, telegram_ids: list[int]) -> dict[int, str]:
    """The stored language of each id that has an account (missing ids just absent).

    One query for a whole recipient list: an admin notice can go to several people,
    and it must not cost a round trip per recipient before it can be sent.
    """
    if not telegram_ids:
        return {}
    rows = await pool.fetch(
        "SELECT telegram_id, language FROM users WHERE telegram_id = ANY($1::bigint[])",
        telegram_ids,
    )
    return {int(row["telegram_id"]): str(row["language"]) for row in rows}


async def language_counts(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """How many users each language has (for the admin panel)."""
    return list(
        await pool.fetch(
            "SELECT language, COUNT(*) AS count FROM users GROUP BY language ORDER BY count DESC"
        )
    )


async def admin_stats(pool: asyncpg.Pool, today: date) -> asyncpg.Record:
    """One row of the numbers an operator asks for first.

    A single round trip on purpose: the panel is opened while downloads are running,
    and seven separate counts would be seven chances to notice the pool is busy.
    """
    return await pool.fetchrow(
        """
        SELECT
            (SELECT COUNT(*) FROM users)                                AS users,
            (SELECT COUNT(*) FROM users WHERE is_premium)               AS premium,
            (SELECT COUNT(*) FROM users WHERE created_at::date = $1)    AS new_users,
            (SELECT COUNT(*) FROM users WHERE last_download_date = $1)  AS active_today,
            (SELECT COALESCE(SUM(daily_downloads), 0) FROM users
              WHERE last_download_date = $1)                            AS downloads_today,
            (SELECT COUNT(*) FROM smart_cache)                          AS cache_rows,
            (SELECT COUNT(*) FROM block_events
              WHERE created_at > now() - interval '24 hours')           AS blocks_24h,
            (SELECT COUNT(*) FROM transactions WHERE status = 'pending') AS pending_txns
        """,
        today,
    )


async def get_user(pool: asyncpg.Pool, telegram_id: int) -> Optional[asyncpg.Record]:
    return await pool.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)


# ---------------------------------------------------------------------------
# subscription_plans
# ---------------------------------------------------------------------------

async def list_plans(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return list(await pool.fetch("SELECT * FROM subscription_plans ORDER BY duration_days"))


async def get_plan(pool: asyncpg.Pool, plan_id: int) -> Optional[asyncpg.Record]:
    return await pool.fetchrow("SELECT * FROM subscription_plans WHERE id = $1", plan_id)


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------

async def create_transaction(
    pool: asyncpg.Pool,
    *,
    telegram_id: int,
    plan_id: int,
    amount: Decimal,
    method: str = "manual",
) -> asyncpg.Record:
    return await pool.fetchrow(
        """
        INSERT INTO transactions (id, telegram_id, plan_id, amount, method)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        uuid.uuid4(),
        telegram_id,
        plan_id,
        amount,
        method,
    )


async def get_transaction(pool: asyncpg.Pool, txn_id: uuid.UUID) -> Optional[asyncpg.Record]:
    return await pool.fetchrow("SELECT * FROM transactions WHERE id = $1", txn_id)


async def attach_receipt(pool: asyncpg.Pool, txn_id: uuid.UUID, photo_file_id: str) -> bool:
    """Attach a receipt photo to a *pending* transaction. False if already processed."""
    result = await pool.execute(
        "UPDATE transactions SET receipt_photo_id = $2 WHERE id = $1 AND status = 'pending'",
        txn_id,
        photo_file_id,
    )
    return result == "UPDATE 1"


async def decide_transaction(
    pool: asyncpg.Pool,
    txn_id: uuid.UUID,
    approved: bool,
) -> Optional[dict[str, Any]]:
    """Finalize a pending transaction; grants/extends premium on approval.

    Returns an outcome dict or None when the transaction doesn't exist or was
    already processed (row-locked, so concurrent admin clicks are safe).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT t.id, t.status, t.telegram_id, t.amount, p.duration_days
                  FROM transactions t
                  JOIN subscription_plans p ON p.id = t.plan_id
                 WHERE t.id = $1
                   FOR UPDATE OF t
                """,
                txn_id,
            )
            if row is None or row["status"] != "pending":
                return None
            new_status = "approved" if approved else "rejected"
            await conn.execute(
                "UPDATE transactions SET status = $2, processed_at = now() WHERE id = $1",
                txn_id,
                new_status,
            )
            outcome: dict[str, Any] = {
                "txn_id": txn_id,
                "status": new_status,
                "telegram_id": row["telegram_id"],
                "amount": row["amount"],
                "duration_days": row["duration_days"],
            }
            if approved:
                await conn.execute(
                    """
                    UPDATE users
                       SET is_premium = TRUE,
                           premium_until = GREATEST(COALESCE(premium_until, now()), now())
                                           + make_interval(days => $2)
                     WHERE telegram_id = $1
                    """,
                    row["telegram_id"],
                    row["duration_days"],
                )
            return outcome


# ---------------------------------------------------------------------------
# daily download quotas
# ---------------------------------------------------------------------------

async def get_daily_usage(pool: asyncpg.Pool, telegram_id: int) -> Optional[asyncpg.Record]:
    return await pool.fetchrow(
        "SELECT daily_downloads, last_download_date FROM users WHERE telegram_id = $1",
        telegram_id,
    )


async def can_claim_download(pool: asyncpg.Pool, telegram_id: int, limit: int, today: date) -> bool:
    """Atomically increment today's counter if the user is under ``limit``.

    Returns True when the slot was claimed (counter already incremented), so
    concurrent workers can't oversell the quota. The counter auto-resets when
    the local date changes.
    """
    row = await pool.fetchrow(
        """
        UPDATE users
           SET daily_downloads = CASE
                    WHEN last_download_date IS DISTINCT FROM $2::date THEN 1
                    ELSE daily_downloads + 1
                END,
               last_download_date = $2::date
         WHERE telegram_id = $1
           AND (last_download_date IS DISTINCT FROM $2::date OR daily_downloads < $3)
        RETURNING daily_downloads
        """,
        telegram_id,
        today,
        limit,
    )
    return row is not None


# ---------------------------------------------------------------------------
# smart_cache
# ---------------------------------------------------------------------------

async def get_cached_file(pool: asyncpg.Pool, url_hash: str) -> Optional[asyncpg.Record]:
    return await pool.fetchrow("SELECT * FROM smart_cache WHERE url_hash = $1", url_hash)


async def store_cached_file(
    pool: asyncpg.Pool,
    *,
    url_hash: str,
    original_url: str,
    platform: str,
    telegram_file_id: str,
    quality: str,
    kind: str = "",
) -> None:
    """Remember an upload. ``quality`` is the requested format (part of the key);
    ``kind`` is how to send it again (``photo_group``, ``audio``, …)."""
    await pool.execute(
        """
        INSERT INTO smart_cache (url_hash, original_url, platform, telegram_file_id, quality, kind)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (url_hash) DO NOTHING
        """,
        url_hash,
        original_url,
        platform,
        telegram_file_id,
        quality,
        kind or None,
    )


async def delete_cached_file(pool: asyncpg.Pool, url_hash: str) -> None:
    await pool.execute("DELETE FROM smart_cache WHERE url_hash = $1", url_hash)


# ---------------------------------------------------------------------------
# maintenance
# ---------------------------------------------------------------------------

async def expire_premiums(pool: asyncpg.Pool) -> int:
    """Flip expired premium flags off; returns the number of accounts expired."""
    return await pool.fetchval(
        """
        WITH expired AS (
            UPDATE users
               SET is_premium = FALSE
             WHERE is_premium
               AND premium_until IS NOT NULL
               AND premium_until < now()
            RETURNING telegram_id
        )
        SELECT COUNT(*) FROM expired
        """
    )


# ---------------------------------------------------------------------------
# block_events / bot_state (telemetry)
# ---------------------------------------------------------------------------

async def record_block_event(
    pool: asyncpg.Pool,
    *,
    telegram_id: int | None,
    url_host: str,
    code: str,
    cause: str,
) -> None:
    """One failed download, with the cause that was diagnosed for it."""
    await pool.execute(
        "INSERT INTO block_events (telegram_id, url_host, code, cause) VALUES ($1, $2, $3, $4)",
        telegram_id,
        url_host,
        code,
        cause,
    )


async def block_counts(
    pool: asyncpg.Pool, since: datetime, until: datetime | None = None
) -> dict[str, int]:
    """Failures per cause inside a window (missing causes are simply absent)."""
    rows = await pool.fetch(
        """
        SELECT cause, COUNT(*) AS count
          FROM block_events
         WHERE created_at >= $1 AND created_at < $2
         GROUP BY cause
        """,
        since,
        until or utcnow(),
    )
    return {row["cause"]: int(row["count"]) for row in rows}


async def top_block_host(
    pool: asyncpg.Pool, since: datetime, until: datetime | None = None
) -> tuple[str, int] | None:
    """The host that failed most often in a window, or ``None`` when silent."""
    row = await pool.fetchrow(
        """
        SELECT url_host, COUNT(*) AS count
          FROM block_events
         WHERE created_at >= $1 AND created_at < $2
         GROUP BY url_host
         ORDER BY count DESC, url_host
         LIMIT 1
        """,
        since,
        until or utcnow(),
    )
    return (row["url_host"], int(row["count"])) if row else None


async def prune_block_events(pool: asyncpg.Pool, keep_days: int = 90) -> int:
    """Drop telemetry older than the window a digest can still be asked about."""
    status = await pool.execute(
        "DELETE FROM block_events WHERE created_at < now() - make_interval(days => $1)",
        keep_days,
    )
    return int(status.rsplit(" ", 1)[-1])  # "DELETE 12"


async def blocks_per_day(
    pool: asyncpg.Pool, since: datetime, timezone_name: str
) -> list[tuple[date, str, int]]:
    """Failures per local day per cause — the shape a trend needs.

    Grouped in the configured timezone, not UTC: "the day the fix landed" has to
    mean the operator's day, or the before/after comparison is off by hours.
    """
    rows = await pool.fetch(
        """
        SELECT (created_at AT TIME ZONE $2)::date AS day, cause, COUNT(*) AS count
          FROM block_events
         WHERE created_at >= $1
         GROUP BY day, cause
         ORDER BY day
        """,
        since,
        timezone_name,
    )
    return [(row["day"], row["cause"], int(row["count"])) for row in rows]


async def record_fix_event(pool: asyncpg.Pool, *, kind: str, detail: str = "") -> None:
    """Remember that something was fixed, so a trend can weigh it."""
    await pool.execute(
        "INSERT INTO fix_events (kind, detail) VALUES ($1, $2)", kind, detail
    )


async def record_helper_event(
    pool: asyncpg.Pool, *, helper: str, state: str, reason: str = ""
) -> None:
    """One helper server changed state (or was seen for the first time)."""
    await pool.execute(
        "INSERT INTO helper_events (helper, state, reason) VALUES ($1, $2, $3)",
        helper,
        state,
        reason,
    )


async def last_helper_event(pool: asyncpg.Pool, helper: str) -> tuple[str, str] | None:
    """The most recent recorded state of one helper: ``(state, reason)``."""
    row = await pool.fetchrow(
        """
        SELECT state, reason
          FROM helper_events
         WHERE helper = $1
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        helper,
    )
    return (row["state"], row["reason"]) if row else None


async def helper_events(
    pool: asyncpg.Pool, since: datetime, until: datetime | None = None
) -> list[tuple[datetime, str, str, str]]:
    """Every recorded transition in a window, oldest first."""
    rows = await pool.fetch(
        """
        SELECT created_at, helper, state, reason
          FROM helper_events
         WHERE created_at >= $1 AND created_at < $2
         ORDER BY created_at, id
        """,
        since,
        until or utcnow(),
    )
    return [
        (row["created_at"], row["helper"], row["state"], row["reason"]) for row in rows
    ]


async def prune_helper_events(pool: asyncpg.Pool, keep_days: int = 90) -> int:
    """Drop helper history older than the window a report can still be asked about."""
    status = await pool.execute(
        "DELETE FROM helper_events WHERE created_at < now() - make_interval(days => $1)",
        keep_days,
    )
    return int(status.rsplit(" ", 1)[-1])


async def latest_fix_event(
    pool: asyncpg.Pool, kind: str | None = None
) -> tuple[datetime, str, str] | None:
    """The most recent fix (optionally of one kind): ``(when, kind, detail)``."""
    row = await pool.fetchrow(
        """
        SELECT created_at, kind, detail
          FROM fix_events
         WHERE ($1::text IS NULL OR kind = $1)
         ORDER BY created_at DESC
         LIMIT 1
        """,
        kind,
    )
    return (row["created_at"], row["kind"], row["detail"]) if row else None


async def count_fix_events(pool: asyncpg.Pool, since: datetime | None = None) -> int:
    """How many fixes were recorded (optionally since a moment) — for diagnostics."""
    return int(
        await pool.fetchval(
            """
            SELECT COUNT(*) FROM fix_events
             WHERE ($1::timestamptz IS NULL OR created_at >= $1)
            """,
            since,
        )
    )


async def prune_fix_events(pool: asyncpg.Pool, keep_days: int = 90) -> int:
    """Drop fix records older than the window a trend can be asked about."""
    status = await pool.execute(
        "DELETE FROM fix_events WHERE created_at < now() - make_interval(days => $1)",
        keep_days,
    )
    return int(status.rsplit(" ", 1)[-1])  # "DELETE 12"


async def get_state(pool: asyncpg.Pool, key: str) -> str | None:
    """A remembered value (e.g. when the weekly digest was last sent)."""
    return await pool.fetchval("SELECT value FROM bot_state WHERE key = $1", key)


# ---------------------------------------------------------------------------
# support contact (the user menu's one operator-editable button)
# ---------------------------------------------------------------------------

#: Where the configured support link lives in ``bot_state``.
SUPPORT_CONTACT_KEY = "support_contact"


async def get_support_contact(pool: asyncpg.Pool) -> str:
    """The support link / @username an operator configured (``""`` when unset).

    Never raises. This is read while the *main menu* is being drawn, and a menu
    that fails to appear because one optional row could not be read is a worse bug
    than a button that is missing — the operator sees the failure in the log and
    the user still gets a working bot.
    """
    try:
        return str(await get_state(pool, SUPPORT_CONTACT_KEY) or "").strip()
    except Exception:
        logger.exception("could not read the support contact from bot_state")
        return ""


async def set_support_contact(pool: asyncpg.Pool, value: str) -> None:
    """Store the support contact (``""`` removes the button)."""
    await set_state(pool, SUPPORT_CONTACT_KEY, value.strip())


# ---------------------------------------------------------------------------
# broadcast (every user, one message)
# ---------------------------------------------------------------------------

#: How many recipients one query returns. The broadcast walks pages instead of
#: loading every id at once: a deployment with a million users must not need a
#: million integers in memory to send one announcement.
BROADCAST_PAGE_SIZE = 500


async def user_id_page(
    pool: asyncpg.Pool, after: int = 0, limit: int = BROADCAST_PAGE_SIZE
) -> list[int]:
    """One ordered page of user ids, keyed past ``after`` (``[]`` at the end)."""
    rows = await pool.fetch(
        """
        SELECT telegram_id FROM users
         WHERE telegram_id > $1
         ORDER BY telegram_id
         LIMIT $2
        """,
        after,
        limit,
    )
    return [int(row["telegram_id"]) for row in rows]


async def count_users(pool: asyncpg.Pool) -> int:
    """How many accounts the bot has (what a broadcast is about to reach)."""
    return int(await pool.fetchval("SELECT COUNT(*) FROM users"))


async def set_state(pool: asyncpg.Pool, key: str, value: str) -> None:
    await pool.execute(
        """
        INSERT INTO bot_state (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        value,
    )
