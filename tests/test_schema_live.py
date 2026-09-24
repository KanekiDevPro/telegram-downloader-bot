"""Migration safety against real PostgreSQL — opt-in, never in the unit suite.

The normal suite is offline (see ``tests/test_schema_safety.py`` for the
statement-semantics proof). This module proves the same three startup shapes on
a real server — **fresh database**, **pre-upgrade database**, **repeated
initialization** — plus the rollback contract (a text reset is a DELETE, rows
are the overrides).

It needs an explicit opt-in and a *disposable* database: the test creates and
drops two schemas named ``migration_check_*`` inside it and touches nothing
else (no other schema, no existing table, no data).

    MIGRATION_TEST_DATABASE_URL=postgresql://user:pass@localhost/scratch \\
        python -m pytest tests/test_schema_live.py
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from core.database import (
    SCHEMA_SQL,
    init_db,
    reset_text_override,
    set_text_override,
    text_overrides,
)

DSN = os.getenv("MIGRATION_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set MIGRATION_TEST_DATABASE_URL to a disposable database to run the live proof",
)

FRESH_SCHEMA = "migration_check_fresh"
PRE_SCHEMA = "migration_check_pre"
_SCHEMAS = (FRESH_SCHEMA, PRE_SCHEMA)

#: The previous release's schema: this one minus this release's one addition
#: (``bot_texts``, the last statement in the file).
_PRE_UPGRADE_SQL = SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS bot_texts")[0]


async def _connect(schema: str) -> asyncpg.Connection:
    return await asyncpg.connect(DSN, server_settings={"search_path": schema})


async def _columns(conn: asyncpg.Connection, schema: str) -> dict[str, set[str]]:
    rows = await conn.fetch(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = $1
        """,
        schema,
    )
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["table_name"], set()).add(row["column_name"])
    return out


async def _prepare_schemas() -> None:
    server = await asyncpg.connect(DSN)
    try:
        for name in _SCHEMAS:
            await server.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
            await server.execute(f"CREATE SCHEMA {name}")
    finally:
        await server.close()


async def _drop_schemas() -> None:
    server = await asyncpg.connect(DSN)
    try:
        for name in _SCHEMAS:
            await server.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    finally:
        await server.close()


async def test_fresh_pre_upgrade_and_repeated_initialization_on_real_postgres() -> None:
    await _prepare_schemas()
    try:
        # ---- (a) a fresh database: one init_db gets the whole schema --------
        fresh = await _connect(FRESH_SCHEMA)
        try:
            await init_db(fresh)
            columns = await _columns(fresh, FRESH_SCHEMA)
            assert "bot_texts" in columns
            assert {"key", "lang", "value", "updated_by", "updated_at"} <= columns[
                "bot_texts"
            ]

            # ---- (c) repeated initialization: same schema, no new columns ---
            before = await _columns(fresh, FRESH_SCHEMA)
            await init_db(fresh)
            await init_db(fresh)
            assert await _columns(fresh, FRESH_SCHEMA) == before
            plans = await fresh.fetchval("SELECT COUNT(*) FROM subscription_plans")
            assert plans == 3, "the one-shot seeding did not grow either"
        finally:
            await fresh.close()

        # ---- (b) a pre-upgrade database ------------------------------------
        pre = await _connect(PRE_SCHEMA)
        try:
            await pre.execute(_PRE_UPGRADE_SQL)
            pre_columns = await _columns(pre, PRE_SCHEMA)
            assert "bot_texts" not in pre_columns, "the fixture really is pre-upgrade"
            await pre.execute(
                "INSERT INTO bot_state (key, value) VALUES ('probe', 'kept')"
            )

            await init_db(pre)  # …the upgrade runs
            upgraded = await _columns(pre, PRE_SCHEMA)
            assert "bot_texts" in upgraded, "the new table arrives"
            for table, cols in pre_columns.items():
                assert upgraded.get(table) == cols, f"{table} was rewritten, not preserved"
            assert await pre.fetchval(
                "SELECT value FROM bot_state WHERE key = 'probe'"
            ) == "kept", "existing rows survive the upgrade"

            # ---- repeated init on the upgraded database --------------------
            await init_db(pre)
            assert await _columns(pre, PRE_SCHEMA) == upgraded

            # ---- the rollback contract: reset = DELETE, rows = overrides ----
            await set_text_override(pre, "intake.probe_failed", "en", "Edited")
            rows = await text_overrides(pre)
            assert [dict(row) for row in rows if row["key"] == "intake.probe_failed"]
            await reset_text_override(pre, "intake.probe_failed", "en")
            rows = await text_overrides(pre)
            assert not [row for row in rows if row["key"] == "intake.probe_failed"], (
                "one DELETE puts the default back — no DROP, no history to reconcile"
            )
        finally:
            await pre.close()
    finally:
        await _drop_schemas()
