"""The guard against the real Redis contract (guardfix).

The production defect: ``LEASE_TTL_S`` was a float, and redis-py raises
``DataError`` on any ``EX`` that is not ``int``/``timedelta`` — so the guard
never wrote its lease while every hand-rolled fake (which accepts anything)
stayed green. These tests pin the contract:

* a strict fake mirroring redis-py's ``EX`` rule fails the old code and
  passes the fixed one, for both ``str`` and ``bytes`` clients;
* guard errors log exactly one warning (and one info on recovery);
* a non-reachability error never feeds the Redis-down streak, while a
  genuine ``ConnectionError`` still pages after a sustained outage;
* a real-Redis lifecycle test, skipped unless GUARD_TEST_REDIS_URL is set.

Running the real-Redis suites (C4): each suite reads its own variable and
skips when it is unset — never defaulting to localhost:6379, which on a
developer machine may be a production Redis, not a disposable one:

* GUARD_TEST_REDIS_URL (this file — the instance-lease lifecycle),
* SHZ_TEST_REDIS_URL (tests/test_song_id.py),
* FORCE_JOIN_TEST_REDIS_URL (tests/test_force_join.py),
* INLINE_TEST_REDIS_URL (tests/test_inline.py).

Point each at a throwaway server (e.g. a ``redis:7-alpine`` container started
just for the run, ideally with a per-suite db index); the suites touch only
their own keys and clean up afterwards.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any

import pytest
import redis.exceptions as redis_errors

import services.telemetry as telemetry_module  # noqa: F401  (imported first: breaks the doctor cycle for standalone runs)
from services import instance_guard as guard_module
from services.instance_guard import LEASE_KEY, InstanceGuard

T0 = 2_000_000.0


class _StrictFakeRedis:
    """Accepts ``EX`` exactly like redis-py 8.1.0 (int/timedelta/digit-str)."""

    returns_bytes = False

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ex_args: list[Any] = []

    def _encode(self, value: str) -> Any:
        if self.returns_bytes:
            return value.encode("utf-8")
        return value

    async def get(self, key: str) -> Any:
        value = self.values.get(key)
        return None if value is None else self._encode(value)

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        xx: bool = False,
        ex: Any = None,
    ) -> bool | None:
        if isinstance(ex, datetime.timedelta):
            ex = int(ex.total_seconds())
        elif isinstance(ex, bool) or not isinstance(ex, int):
            if not (isinstance(ex, str) and ex.isdigit()):
                raise redis_errors.DataError("ex must be datetime.timedelta or int")
            ex = int(ex)
        self.ex_args.append(ex)
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def ttl(self, key: str) -> int:
        return 60 if key in self.values else -2

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


class _StrictBytesFakeRedis(_StrictFakeRedis):
    returns_bytes = True


class _BrokenRedis:
    """Every op raises a non-reachability error (a bug, not an outage)."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def get(self, key: str) -> Any:
        raise self.error

    async def set(self, key: str, value: str, **kwargs: Any) -> Any:
        raise self.error

    async def ttl(self, key: str) -> Any:
        raise self.error


class _DownRedis(_BrokenRedis):
    def __init__(self) -> None:
        super().__init__(redis_errors.ConnectionError("redis is down"))


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "instance-lease" in record.message
    ]


def _infos(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelname == "INFO" and "instance-lease ok" in record.message
    ]


# ---------------------------------------------------------------------------
# The contract: integer TTL, both client decodings
# ---------------------------------------------------------------------------


def test_the_lease_ttl_is_an_integer_for_redis() -> None:
    assert isinstance(guard_module.LEASE_TTL_S, int), (
        "SET EX rejects floats client-side (DataError) — the lease would never exist"
    )


@pytest.mark.parametrize("client_cls", [_StrictFakeRedis, _StrictBytesFakeRedis])
async def test_start_writes_a_lease_on_either_client_decoding(client_cls: type) -> None:
    redis = client_cls()
    guard = await guard_module.start_guard(redis, grace_s=0)

    assert guard.state == "single"
    assert LEASE_KEY in redis.values
    assert redis.ex_args and all(isinstance(arg, int) for arg in redis.ex_args)
    assert await redis.ttl(LEASE_KEY) == 60


# ---------------------------------------------------------------------------
# Observability: exactly one warning, one info on recovery
# ---------------------------------------------------------------------------


async def test_guard_errors_warn_exactly_once(caplog: pytest.LogCaptureFixture) -> None:
    redis: Any = _BrokenRedis(RuntimeError("boom"))
    guard = await guard_module.start_guard(redis, grace_s=0)
    assert guard.state == "unknown"
    assert len(_warnings(caplog)) == 1
    assert "RuntimeError" in _warnings(caplog)[0].message

    await guard.refresh_once(now=T0)
    await guard.refresh_once(now=T0 + 20.0)
    await guard.refresh_once(now=T0 + 40.0)
    assert len(_warnings(caplog)) == 1, "repeated ticks stay silent"


async def test_recovery_logs_one_info_and_re_arms_the_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="services.instance_guard")
    redis = _StrictFakeRedis()
    guard = await guard_module.start_guard(redis, grace_s=0)
    assert guard.state == "single"

    guard._redis = _BrokenRedis(RuntimeError("boom"))
    await guard.refresh_once(now=T0)
    assert guard.state == "unknown"
    assert len(_warnings(caplog)) == 1

    guard._redis = redis
    await guard.refresh_once(now=T0 + 20.0)
    assert guard.state == "single"
    assert len(_infos(caplog)) == 1, "one info line when it becomes ok"

    guard._redis = _BrokenRedis(RuntimeError("boom again"))
    await guard.refresh_once(now=T0 + 40.0)
    assert len(_warnings(caplog)) == 2, "a new degradation warns again"


# ---------------------------------------------------------------------------
# Streak decoupling: bugs never page, real outages still do
# ---------------------------------------------------------------------------


async def test_a_programming_error_never_pages() -> None:
    redis: Any = _BrokenRedis(RuntimeError("boom"))
    guard = await guard_module.start_guard(redis, grace_s=0)
    calls: list[int] = []

    async def _page() -> None:
        calls.append(1)

    for tick in (0.0, 20.0, 40.0, 60.0, 80.0, 400.0):
        await guard.refresh_once(on_redis_down=_page, now=T0 + tick)

    assert calls == [], "a non-reachability error must not feed the outage streak"
    assert guard.state == "unknown"


async def test_a_genuine_outage_still_pages_after_a_sustained_streak() -> None:
    redis: Any = _DownRedis()
    guard = await guard_module.start_guard(redis, grace_s=0)
    calls: list[int] = []

    async def _page() -> None:
        calls.append(1)

    await guard.refresh_once(on_redis_down=_page, now=T0)
    await guard.refresh_once(on_redis_down=_page, now=T0 + 40.0)
    assert calls == [], "under 60s of failure stays silent"
    await guard.refresh_once(on_redis_down=_page, now=T0 + 60.0)
    assert calls == [1], "a sustained unreachable streak still pages"


# ---------------------------------------------------------------------------
# Real Redis: skipped unless GUARD_TEST_REDIS_URL points at a disposable
# server (never localhost by default — see the module docstring).
# ---------------------------------------------------------------------------


def _real_redis_url() -> str | None:
    return os.getenv("GUARD_TEST_REDIS_URL", "") or None


async def _real_client() -> Any:
    import redis.asyncio as aioredis

    url = _real_redis_url()
    if not url:
        pytest.skip("set GUARD_TEST_REDIS_URL to a disposable Redis")
    client = aioredis.from_url(url, decode_responses=True, socket_timeout=1.0)
    await client.ping()
    return client


async def test_lease_lifecycle_against_real_redis() -> None:
    client = await _real_client()
    try:
        first = await guard_module.start_guard(client, grace_s=0)
        assert first.state == "single"

        raw = await client.get(LEASE_KEY)
        assert isinstance(raw, str) and "|" in raw
        ttl = await client.ttl(LEASE_KEY)
        assert isinstance(ttl, int) and 0 < ttl <= 60

        second: InstanceGuard = await guard_module.start_guard(client, grace_s=0)
        assert second.state == "duplicate", "a live foreign lease is seen, not stolen"
    finally:
        try:
            await client.delete(LEASE_KEY)
        except Exception:
            pass
        try:
            await client.aclose()
        except Exception:
            pass
