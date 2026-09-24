"""Migration safety of the idempotent schema: additive, repeatable, rollback-safe.

There are no migration files — ``core.database.SCHEMA_SQL`` is re-run at every
boot — so the safety argument is about the *statements themselves*, and that is
what is pinned here at statement-semantics level (no Postgres needed): the DDL
is additive-only, a fresh database gets every table, a **pre-upgrade** database
(the previous release's schema) only gains what this release adds —
``bot_texts`` — and keeps every table and column it had, and repeated
initialization is a no-op. Rollback needs no ``DROP`` anywhere: a text reset is
one ``DELETE`` and the table simply sits unused.

The same three startup shapes are also proven against real PostgreSQL —
opt-in, outside the unit suite — by ``tests/test_schema_live.py``.
"""

from __future__ import annotations

import re
from typing import Any

from core.database import SCHEMA_SQL, SEED_PLANS, init_db, reset_text_override

_COMMENT = re.compile(r"--[^\n]*")
_DO_BLOCK = re.compile(r"DO \$\$.*?\$\$;", re.S)
_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*)\)\s*", re.S)
_CREATE_INDEX = re.compile(r"CREATE INDEX IF NOT EXISTS (\w+)\s+ON (\w+)")
_ADD_COLUMN = re.compile(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)")
_CREATE_TYPE = re.compile(r"CREATE TYPE (\w+)")

#: Table-level constraints, not columns (bot_texts ends with one).
_NOT_A_COLUMN = (
    "PRIMARY KEY",
    "FOREIGN",
    "UNIQUE",
    "CHECK",
    "CONSTRAINT",
    "REFERENCES",
)


def _statements(sql: str) -> list[str]:
    """The schema's statements — comments stripped, DO blocks kept whole."""
    sql = _COMMENT.sub("", sql)
    blocks = _DO_BLOCK.findall(sql)
    rest = _DO_BLOCK.sub("", sql)
    return [stmt.strip() for stmt in blocks + rest.split(";") if stmt.strip()]


def _columns(body: str) -> set[str]:
    names: set[str] = set()
    for line in body.splitlines():
        line = line.strip()
        if not line or line.upper().startswith(_NOT_A_COLUMN):
            continue
        names.add(line.split()[0])
    return names


class Catalog:
    """A tiny model of a Postgres catalog — what these statements *do*.

    Deliberately strict: anything that is not an additive statement raises, so
    "the schema is additive" is not a grep opinion but the only thing this
    applier can execute. ``CREATE TABLE IF NOT EXISTS`` on an existing table is
    a no-op (that is why the ``ADD COLUMN`` guards exist), and the model
    behaves exactly so.
    """

    def __init__(self) -> None:
        self.tables: dict[str, set[str]] = {}
        self.indexes: set[str] = set()
        self.types: set[str] = set()

    def apply(self, sql: str) -> None:
        for stmt in _statements(sql):
            if stmt.startswith("DO "):
                match = _CREATE_TYPE.search(stmt)
                if match:
                    self.types.add(match.group(1))
                continue
            if stmt.startswith("CREATE TABLE"):
                match = _CREATE_TABLE.match(stmt)
                assert match is not None, stmt[:80]
                name, body = match.group(1), match.group(2)
                if name not in self.tables:  # IF NOT EXISTS on a table is a no-op
                    self.tables[name] = _columns(body)
                continue
            if stmt.startswith("CREATE INDEX"):
                match = _CREATE_INDEX.match(stmt)
                assert match is not None, stmt[:80]
                self.indexes.add(match.group(1))
                continue
            if stmt.startswith("ALTER TABLE"):
                match = _ADD_COLUMN.match(stmt)
                assert match is not None, f"non-additive ALTER: {stmt[:80]}"
                self.tables[match.group(1)].add(match.group(2))
                continue
            raise AssertionError(f"unexpected schema statement: {stmt[:80]}")


def _pre_upgrade_sql() -> str:
    """The previous release's schema: this one minus this release's additions.

    ``bot_texts`` is the only statement this release adds and it is the last in
    the file, so the pre-upgrade database is exactly what ran before it — every
    other statement is unchanged history.
    """
    return SCHEMA_SQL.split("CREATE TABLE IF NOT EXISTS bot_texts")[0]


# ---------------------------------------------------------------------------
# Additive-only
# ---------------------------------------------------------------------------


def test_the_schema_contains_no_destructive_statement() -> None:
    bare = _COMMENT.sub("", SCHEMA_SQL).upper()
    for forbidden in ("DROP ", "TRUNCATE", "DELETE FROM", "RENAME", "ALTER COLUMN", "UPDATE "):
        assert forbidden not in bare, forbidden
    # …and the strict applier below accepts every statement (it raises on
    # anything that is not additive): a fresh catalog takes the whole schema.
    Catalog().apply(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# a. a fresh database
# ---------------------------------------------------------------------------


def test_a_fresh_database_gets_every_table() -> None:
    catalog = Catalog()
    catalog.apply(SCHEMA_SQL)
    assert {"users", "smart_cache", "bot_texts", "fix_events", "bot_state"} <= set(
        catalog.tables
    )
    assert catalog.tables["bot_texts"] == {
        "key",
        "lang",
        "value",
        "updated_by",
        "updated_at",
    }
    assert {"transaction_status", "payment_method"} <= catalog.types


# ---------------------------------------------------------------------------
# b. a pre-upgrade database
# ---------------------------------------------------------------------------


def test_a_pre_upgrade_database_only_gains_bot_texts() -> None:
    pre = Catalog()
    pre.apply(_pre_upgrade_sql())
    assert "bot_texts" not in pre.tables, "the fixture really is pre-upgrade"

    snapshot = {name: set(cols) for name, cols in pre.tables.items()}
    pre.apply(SCHEMA_SQL)

    assert "bot_texts" in pre.tables, "the upgrade adds the new table"
    assert set(snapshot) <= set(pre.tables), "and drops nothing"
    for name, cols in snapshot.items():
        assert pre.tables[name] == cols, f"{name} was rewritten, not preserved"


# ---------------------------------------------------------------------------
# c. repeated initialization
# ---------------------------------------------------------------------------


def test_repeated_initialization_is_a_no_op() -> None:
    once = Catalog()
    once.apply(SCHEMA_SQL)
    twice = Catalog()
    twice.apply(SCHEMA_SQL)
    twice.apply(SCHEMA_SQL)
    assert twice.tables == once.tables
    assert twice.indexes == once.indexes
    assert twice.types == once.types


async def test_repeated_boot_seeds_the_plans_once() -> None:
    """init_db at the function level: the DDL runs every time, the seeding does
    not grow."""

    class FakePool:
        def __init__(self) -> None:
            self.schema_runs = 0
            self.plans = 0

        async def execute(self, sql: str, *args: Any) -> str:
            self.schema_runs += 1
            return "OK"

        async def fetchval(self, sql: str, *args: Any) -> int:
            return self.plans

        async def executemany(self, sql: str, rows: list[Any]) -> None:
            self.plans += len(rows)

    pool = FakePool()
    await init_db(pool)
    await init_db(pool)
    assert pool.schema_runs == 2, "the idempotent DDL is applied every boot"
    assert pool.plans == len(SEED_PLANS), "but the one-shot seeding happens once"


# ---------------------------------------------------------------------------
# Rollback: no DROP required, ever
# ---------------------------------------------------------------------------


async def test_a_text_reset_is_one_delete_and_rollback_needs_no_drop() -> None:
    """The rollback contract: old code ignores ``bot_texts`` entirely, and
    resetting texts is a DELETE (rows = overrides). Nothing anywhere needs a
    ``DROP TABLE`` to roll back."""

    class Recorder:
        def __init__(self) -> None:
            self.sql: list[tuple[str, tuple[Any, ...]]] = []

        async def execute(self, sql: str, *args: Any) -> str:
            self.sql.append((sql, args))
            return "DELETE 1"

    pool = Recorder()
    await reset_text_override(pool, "intake.probe_failed", "en")
    assert pool.sql, "something was executed"
    statement = pool.sql[0][0].upper()
    assert statement.startswith("DELETE FROM BOT_TEXTS")
    assert "DROP" not in statement
    assert "DROP " not in _COMMENT.sub("", SCHEMA_SQL).upper()
