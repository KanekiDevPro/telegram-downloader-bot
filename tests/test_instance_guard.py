"""Single-instance tripwire (P0-3): two live bot processes must never silently
share one Redis.

Session modes, the double-tap guard and the claim shelf are per-process, so a
second live instance forks all of them. The guard holds a best-effort Redis
lease (unique id, TTL, refresh); a live foreign lease means warn loudly (log +
/doctor row), never fail startup. A stale foreign lease (restart-after-crash)
is taken over silently after a grace period plus a re-check - never mistaken
for a live peer. No Redis (memory mode) means disabled: single-process by
definition.
"""

from __future__ import annotations

import asyncio

from services import instance_guard as guard_module
from services.instance_guard import InstanceGuard


class FakeRedis:
    """Redis with hand-driven TTLs: the test decides what is fresh or stale."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, float] = {}
        self.down = False
        self.writes: list[tuple[str, str]] = []

    async def get(self, key: str) -> str | None:
        if self.down:
            raise RuntimeError("redis is down")
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        xx: bool = False,
        ex: float | None = None,
    ) -> bool | None:
        if self.down:
            raise RuntimeError("redis is down")
        if nx and key in self.values:
            return None
        if xx and key not in self.values:
            return None
        self.values[key] = value
        self.writes.append((key, value))
        if ex is not None:
            self.ttls[key] = float(ex)
        return True

    async def ttl(self, key: str) -> int:
        if self.down:
            raise RuntimeError("redis is down")
        if key not in self.values:
            return -2
        return int(self.ttls.get(key, -1))

    async def delete(self, key: str) -> int:
        if self.down:
            raise RuntimeError("redis is down")
        self.writes.append((key, ""))
        if key in self.values:
            del self.values[key]
            self.ttls.pop(key, None)
            return 1
        return 0

    def hold_foreign(self, instance_id: str, ttl: float) -> None:
        self.values[guard_module.LEASE_KEY] = f"{instance_id}|1000"
        self.ttls[guard_module.LEASE_KEY] = ttl


def _lease_owner(redis: FakeRedis) -> str:
    return (redis.values.get(guard_module.LEASE_KEY) or "").split("|")[0]


async def test_single_start_claims_the_lease() -> None:
    redis = FakeRedis()

    guard = await guard_module.start_guard(redis, grace_s=0)

    assert guard.state == "single"
    assert _lease_owner(redis) == guard.instance_id
    assert redis.ttls[guard_module.LEASE_KEY] == guard_module.LEASE_TTL_S


async def test_live_foreign_lease_means_duplicate_without_touching_it() -> None:
    redis = FakeRedis()
    redis.hold_foreign("peer-live", ttl=55.0)

    guard = await guard_module.start_guard(redis, grace_s=0)

    assert guard.state == "duplicate"
    assert guard.peer_id == "peer-live"
    assert _lease_owner(redis) == "peer-live", "an observer never overwrites a live lease"


async def test_stale_foreign_lease_is_taken_over_silently() -> None:
    """Restart-after-crash: the old lease was never refreshed, so take it."""
    redis = FakeRedis()
    redis.hold_foreign("peer-dead", ttl=5.0)

    guard = await guard_module.start_guard(redis, grace_s=0)

    assert guard.state == "single"
    assert _lease_owner(redis) == guard.instance_id


async def test_lease_gone_on_recheck_is_claimed() -> None:
    redis = FakeRedis()
    redis.hold_foreign("peer-gone", ttl=55.0)

    guard = await guard_module.start_guard(redis, grace_s=0)
    assert guard.state == "duplicate"
    # The peer exits (or its lease expires) before the next observation.
    redis.values.pop(guard_module.LEASE_KEY, None)
    redis.ttls.pop(guard_module.LEASE_KEY, None)
    await guard.refresh_once()

    assert guard.state == "single"
    assert _lease_owner(redis) == guard.instance_id


async def test_crash_restart_clears_the_transient_duplicate() -> None:
    """A foreign lease with a high TTL that then goes stale was a dead peer:
    the warning must clear itself instead of paging forever."""
    redis = FakeRedis()
    redis.hold_foreign("peer-crashed", ttl=55.0)

    guard = await guard_module.start_guard(redis, grace_s=0)
    assert guard.state == "duplicate"
    redis.ttls[guard_module.LEASE_KEY] = 5.0
    await guard.refresh_once()

    assert guard.state == "single"
    assert _lease_owner(redis) == guard.instance_id


async def test_no_redis_means_disabled_single_process() -> None:
    guard = await guard_module.start_guard(None, grace_s=0)

    assert guard.state == "disabled"
    check = guard.as_check()
    assert check.status == "ok"


async def test_guard_failure_never_blocks_startup() -> None:
    redis = FakeRedis()
    redis.down = True

    guard = await guard_module.start_guard(redis, grace_s=0)

    assert guard.state == "unknown"
    assert guard.as_check().status == "warn"


async def test_graceful_shutdown_releases_only_its_own_lease() -> None:
    redis = FakeRedis()
    guard = await guard_module.start_guard(redis, grace_s=0)
    assert _lease_owner(redis) == guard.instance_id

    await guard.stop()

    assert guard_module.LEASE_KEY not in redis.values

    redis.hold_foreign("someone-else", ttl=55.0)
    await guard.stop()
    assert _lease_owner(redis) == "someone-else"


async def test_holder_refresh_keeps_the_lease_and_loser_re_evaluates() -> None:
    redis = FakeRedis()
    guard = await guard_module.start_guard(redis, grace_s=0)

    await guard.refresh_once()

    assert guard.state == "single"
    assert _lease_owner(redis) == guard.instance_id


async def test_duplicate_is_a_warning_not_a_failure() -> None:
    """Warn-only: a second instance must not flip the /doctor verdict to fail."""
    redis = FakeRedis()
    redis.hold_foreign("peer-live", ttl=55.0)
    guard = await guard_module.start_guard(redis, grace_s=0)

    check = guard.as_check()

    assert check.status == "warn"
    assert "peer-live" in check.detail


async def test_refresh_loop_exits_promptly_on_stop() -> None:
    redis = FakeRedis()
    guard = await guard_module.start_guard(redis, grace_s=0)
    stop = asyncio.Event()

    task = asyncio.create_task(guard.run(stop))
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)


def test_lease_value_carries_no_secrets() -> None:
    """The lease is readable by anyone with Redis access: id + epoch only."""
    value = InstanceGuard._lease_value("abc123", 1700000000.0)

    assert value == "abc123|1700000000"
    parsed = InstanceGuard._parse_lease(value)
    assert parsed == ("abc123", 1700000000.0)
    assert InstanceGuard._parse_lease("garbage") is None
    assert InstanceGuard._parse_lease(None) is None
