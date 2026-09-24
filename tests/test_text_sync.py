"""The text-override store across processes: no restarts, bounded staleness.

The release blocker, pinned: a text saved by one process is live in every other
process without restarting any of them. The mechanism is deliberately boring —
the database is the source of truth, one shared counter (Redis in production,
:class:`core.texts.RedisVersionChannel`) is the invalidation signal, and a
bounded TTL covers a counter nobody can reach. Two independent
:class:`core.texts.OverrideStore` instances over one fake channel/database are
"two processes" here: no shared memory, no monkeypatched module state — just
the same two inputs a real deployment shares.

Also pinned: ``t()`` never touches the database (it is synchronous and hot),
and every failure mode — channel down, database down — is a *state* that keeps
the last known texts live instead of an exception on a user's path.
"""

from __future__ import annotations

from typing import Any

import pytest

from core import texts as text_store
from core.catalog import MESSAGES
from core.i18n import t
from core.texts import OverrideStore, RedisVersionChannel

KEY = "intake.probe_failed"
FIRST = "Could not read this link — try again."
SECOND = "That link did not work — try once more."


class FakeChannel:
    """One shared counter — exactly what RedisVersionChannel wraps."""

    def __init__(self) -> None:
        self.value = 0
        self.down = False

    async def version(self) -> str:
        if self.down:
            raise RuntimeError("redis is down")
        return str(self.value)

    async def bump(self) -> str:
        if self.down:
            raise RuntimeError("redis is down")
        self.value += 1
        return str(self.value)


class FakeDB:
    """``bot_texts`` in memory: the source of truth every process reloads from."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        self.reads = 0
        self.down = False

    async def text_overrides(self) -> list[dict[str, Any]]:
        self.reads += 1
        if self.down:
            raise RuntimeError("database is down")
        return [
            {"key": key, "lang": lang, "value": value}
            for (key, lang), value in self.rows.items()
        ]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _store(db: FakeDB, channel: FakeChannel | None, clock: FakeClock) -> OverrideStore:
    store = OverrideStore(
        ttl_s=text_store.FALLBACK_TTL_S,
        min_interval_s=text_store.SYNC_MIN_INTERVAL_S,
        clock=clock,
    )
    store.bind(db.text_overrides, channel)
    return store


# ---------------------------------------------------------------------------
# Two processes, one truth
# ---------------------------------------------------------------------------


async def test_two_processes_see_a_save_and_a_reset_without_a_restart() -> None:
    """The blocker itself: alpha saves, beta syncs, beta sees it — and the same
    for a reset. No restart, no shared memory: the channel and the database are
    all the two stores share."""
    channel, db, clock = FakeChannel(), FakeDB(), FakeClock()
    alpha = _store(db, channel, clock)
    beta = _store(db, channel, clock)
    await alpha.sync(force=True)
    await beta.sync(force=True)
    assert beta.override_for(KEY, "en") is None

    # Alpha's admin saves: the database first (the source of truth), publish second.
    db.rows[(KEY, "en")] = FIRST
    await alpha.saved(KEY, "en", FIRST)
    assert alpha.override_for(KEY, "en") == FIRST, "the writer sees it immediately"
    assert beta.override_for(KEY, "en") is None, "coherence happens on sync, not telepathy"

    assert await beta.sync(force=True) is True
    assert beta.override_for(KEY, "en") == FIRST

    # …and a reset travels the same road back to the default.
    db.rows.pop((KEY, "en"))
    await alpha.cleared(KEY, "en")
    await beta.sync(force=True)
    assert beta.override_for(KEY, "en") is None
    assert beta.effective(KEY, "en") == MESSAGES[KEY]["en"]


async def test_a_sync_only_reloads_when_the_counter_moved() -> None:
    """The cheap path: an unchanged counter is one GET and no database read."""
    channel, db, clock = FakeChannel(), FakeDB(), FakeClock()
    store = _store(db, channel, clock)
    await store.sync(force=True)
    reads_after_first = db.reads

    clock.tick(text_store.SYNC_MIN_INTERVAL_S + 1)
    assert await store.sync(force=True) is False
    assert db.reads == reads_after_first, "unchanged version → no reload"

    db.rows[(KEY, "en")] = FIRST
    channel.value += 1  # somebody else's save bumped the counter
    assert await store.sync(force=True) is True
    assert db.reads == reads_after_first + 1, "a moved counter is worth exactly one read"


async def test_sync_is_rate_limited_between_checks() -> None:
    channel, db, clock = FakeChannel(), FakeDB(), FakeClock()
    store = _store(db, channel, clock)
    await store.sync(force=True)
    reads = db.reads

    channel.value += 1
    assert await store.sync() is False, "inside the interval nothing is even checked"
    assert db.reads == reads

    clock.tick(text_store.SYNC_MIN_INTERVAL_S + 1)
    assert await store.sync() is True, "…and the next interval picks the change up"


# ---------------------------------------------------------------------------
# Redis down: the TTL bounds staleness; nothing fails
# ---------------------------------------------------------------------------


async def test_when_the_channel_is_down_the_ttl_bounds_staleness() -> None:
    """The documented degraded mode: with no reachable counter, the database is
    re-read at most every FALLBACK_TTL_S seconds — so a save is seen by every
    process within that bound, Redis or no Redis."""
    channel, db, clock = FakeChannel(), FakeDB(), FakeClock()
    channel.down = True
    alpha = _store(db, channel, clock)
    beta = _store(db, channel, clock)
    await alpha.sync(force=True)
    await beta.sync(force=True)

    db.rows[(KEY, "en")] = FIRST
    await alpha.saved(KEY, "en", FIRST)  # publish fails — and must not raise
    assert alpha.override_for(KEY, "en") == FIRST

    assert await beta.sync(force=True) is False, "inside the TTL the snapshot stands"
    clock.tick(text_store.FALLBACK_TTL_S + 1)
    assert await beta.sync(force=True) is True
    assert beta.override_for(KEY, "en") == FIRST, "…and the TTL is the staleness bound"


async def test_a_failed_publish_still_takes_the_change_live_locally() -> None:
    channel, db, clock = FakeChannel(), FakeDB(), FakeClock()
    channel.down = True
    store = _store(db, channel, clock)
    await store.sync(force=True)

    db.rows[(KEY, "en")] = FIRST
    await store.saved(KEY, "en", FIRST)  # no raise, no swallowed state
    assert store.override_for(KEY, "en") == FIRST

    channel.down = False  # the channel returns; the next sync re-converges
    clock.tick(text_store.SYNC_MIN_INTERVAL_S + 1)
    await store.sync(force=True)
    assert store.override_for(KEY, "en") == FIRST


async def test_a_failing_database_keeps_the_last_known_texts() -> None:
    """A reload that fails is a state, not an outage: the old text stays live
    (an old sentence beats a broken chat), and no exception reaches a handler."""
    channel, db, clock = FakeChannel(), FakeDB(), FakeClock()
    store = _store(db, channel, clock)
    db.rows[(KEY, "en")] = FIRST
    await store.sync(force=True)

    db.down = True
    channel.value += 1
    clock.tick(text_store.SYNC_MIN_INTERVAL_S + 1)
    assert await store.sync(force=True) is False
    assert store.override_for(KEY, "en") == FIRST, "the snapshot survives the outage"

    db.down = False
    db.rows[(KEY, "en")] = SECOND
    assert await store.sync(force=True) is True
    assert store.override_for(KEY, "en") == SECOND


# ---------------------------------------------------------------------------
# t() is synchronous, hot, and database-free
# ---------------------------------------------------------------------------


async def test_a_text_lookup_never_touches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the contract: coherence is async work done elsewhere —
    ``t()`` reads memory and nothing else, so no lookup can ever cost a query."""
    db = FakeDB()
    monkeypatch.setattr(text_store, "_store", OverrideStore())
    text_store.bind_store(db.text_overrides, None)
    text_store.apply_overrides([{"key": KEY, "lang": "en", "value": FIRST}])

    for _ in range(50):
        assert t(KEY, "en") == FIRST
    assert db.reads == 0, "fifty lookups, zero database reads"


# ---------------------------------------------------------------------------
# The channel itself: one counter, one key
# ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.ops: list[str] = []

    async def get(self, key: str) -> bytes | None:
        self.ops.append(f"get {key}")
        return self.kv.get(key)

    async def incr(self, key: str) -> int:
        self.ops.append(f"incr {key}")
        value = int(self.kv.get(key, b"0")) + 1
        self.kv[key] = str(value).encode()
        return value


async def test_the_redis_channel_is_one_counter_on_one_key() -> None:
    redis = FakeRedis()
    channel = RedisVersionChannel(redis)

    assert await channel.version() == "0", "no key yet reads as the first version"
    assert await channel.bump() == "1"
    assert await channel.bump() == "2"
    assert await channel.version() == "2"
    assert all(op.endswith(text_store.VERSION_KEY) for op in redis.ops)
    assert list(redis.kv) == [text_store.VERSION_KEY], "and nothing else is touched"
