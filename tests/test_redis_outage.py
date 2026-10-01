"""Mid-run Redis outage notice (A): one admin page after a sustained outage.

The instance-guard refresh loop already touches Redis every REFRESH_S, so it
is the detector: a continuous failure streak of >= 60s (blips stay silent)
fires a notifier, throttled through the shared bot_state row like the P1-3
boot notice. No new poller, no per-worker streaks, memory mode never fires.
Postgres down means the page is absorbed, never a crash. /doctor already
shows the guard's live state, pinned here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import redis.exceptions as redis_errors

import main as entrypoint
from services import instance_guard as guard_module
from services.instance_guard import InstanceGuard

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ADMINS = (11, 22)
T0 = 1_000_000.0


class _FakeRedis:
    """Up until told otherwise; every op raises once down.

    The failure is a real ``redis`` reachability error — what redis-py raises
    when the server is unreachable — because only unreachable counts toward
    the outage streak (a programming error must never page about Redis).
    """

    def __init__(self) -> None:
        self.down = False

    async def get(self, key: str) -> str | None:
        if self.down:
            raise redis_errors.ConnectionError("redis is down")
        return None

    async def set(self, key: str, value: str, **kwargs: Any) -> bool:
        if self.down:
            raise redis_errors.ConnectionError("redis is down")
        return True

    async def ttl(self, key: str) -> int:
        if self.down:
            raise redis_errors.ConnectionError("redis is down")
        return 60


class _FakePool:
    """The bot_state table as a dict — shared across guards like Postgres."""

    def __init__(self, state: dict[str, str] | None = None) -> None:
        self.state = state if state is not None else {}

    async def fetchval(self, _query: str, key: str) -> str | None:
        return self.state.get(key)

    async def execute(self, _query: str, key: str, value: str) -> None:
        self.state[key] = value


class _FailingPool(_FakePool):
    async def fetchval(self, _query: str, key: str) -> str | None:
        raise ConnectionError("database unreachable")

    async def execute(self, _query: str, key: str, value: str) -> None:
        raise ConnectionError("database unreachable")


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


async def _single_guard(redis: _FakeRedis) -> InstanceGuard:
    guard = await guard_module.start_guard(redis, grace_s=0)
    assert guard.state == "single"
    return guard


# ---------------------------------------------------------------------------
# The streak: blips never page, sustained failure does, recovery resets
# ---------------------------------------------------------------------------


async def test_a_short_blip_never_pages() -> None:
    redis, calls = _FakeRedis(), []
    guard = await _single_guard(redis)

    async def _page() -> None:
        calls.append(1)

    redis.down = True
    await guard.refresh_once(on_redis_down=_page, now=T0)
    await guard.refresh_once(on_redis_down=_page, now=T0 + 59.9)
    redis.down = False
    await guard.refresh_once(on_redis_down=_page, now=T0 + 60.0)

    assert calls == [], "59.9s of failure then recovery is a blip, not an outage"
    assert guard.state == "single"


async def test_sixty_seconds_of_failure_pages_once_per_tick() -> None:
    redis, calls = _FakeRedis(), []
    guard = await _single_guard(redis)

    async def _page() -> None:
        calls.append(1)

    redis.down = True
    await guard.refresh_once(on_redis_down=_page, now=T0)
    await guard.refresh_once(on_redis_down=_page, now=T0 + 20.0)
    await guard.refresh_once(on_redis_down=_page, now=T0 + 40.0)
    assert calls == []
    await guard.refresh_once(on_redis_down=_page, now=T0 + 60.0)
    assert calls == [1], "the streak is continuous for >= 60s"
    await guard.refresh_once(on_redis_down=_page, now=T0 + 80.0)
    assert calls == [1, 1], "level-triggered: still down, still paging (cooldown dedupes)"


async def test_recovery_resets_the_streak() -> None:
    redis, calls = _FakeRedis(), []
    guard = await _single_guard(redis)

    async def _page() -> None:
        calls.append(1)

    redis.down = True
    await guard.refresh_once(on_redis_down=_page, now=T0)
    redis.down = False
    await guard.refresh_once(on_redis_down=_page, now=T0 + 10.0)
    redis.down = True
    await guard.refresh_once(on_redis_down=_page, now=T0 + 50.0)

    assert calls == [], "10s of health restarts the 60s clock from zero"


async def test_healthy_refreshes_never_page() -> None:
    redis, calls = _FakeRedis(), []
    guard = await _single_guard(redis)

    async def _page() -> None:
        calls.append(1)

    await guard.refresh_once(on_redis_down=_page, now=T0)
    await guard.refresh_once(on_redis_down=_page, now=T0 + 3600.0)

    assert calls == []


async def test_memory_mode_never_pages() -> None:
    calls: list[int] = []
    guard = InstanceGuard(None)
    assert guard.state == "disabled"

    async def _page() -> None:
        calls.append(1)

    await guard.refresh_once(on_redis_down=_page, now=T0)
    await guard.refresh_once(on_redis_down=_page, now=T0 + 3600.0)

    assert calls == []
    assert guard.state == "disabled"


async def test_a_throwing_notifier_does_not_break_the_guard() -> None:
    redis = _FakeRedis()
    guard = await _single_guard(redis)

    async def _boom() -> None:
        raise RuntimeError("postgres is down too")

    redis.down = True
    await guard.refresh_once(on_redis_down=_boom, now=T0)
    await guard.refresh_once(on_redis_down=_boom, now=T0 + 61.0)

    assert guard.state == "unknown"


async def test_an_outage_surfaces_in_doctor_as_a_warn_row() -> None:
    """The live signal /doctor already reports: the tripwire row goes warn."""
    redis = _FakeRedis()
    guard = await _single_guard(redis)
    redis.down = True
    await guard.refresh_once(now=T0)

    check = guard.as_check()

    assert check.status == "warn"
    assert "Redis" in check.detail


# ---------------------------------------------------------------------------
# The page: once per window, across guards and restarts, absorbed when down
# ---------------------------------------------------------------------------


async def test_healthy_path_sends_nothing() -> None:
    bot, pool = _FakeBot(), _FakePool()

    sent = await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=False, now=NOW  # type: ignore[arg-type]
    )

    assert sent is False
    assert bot.sent == []
    assert pool.state == {}, "no state touched"


async def test_sustained_outage_pages_admins_once() -> None:
    bot, pool = _FakeBot(), _FakePool()

    sent = await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )

    assert sent is True
    assert [chat for chat, _text in bot.sent] == [11, 22], "admins only"
    assert len(bot.sent) == 2


async def test_repeated_checks_inside_the_window_stay_silent() -> None:
    bot, pool = _FakeBot(), _FakePool()

    assert await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    ) is True
    assert await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    ) is False
    assert await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW + timedelta(hours=5)  # type: ignore[arg-type]
    ) is False

    assert len(bot.sent) == 2, "one page per window"


async def test_two_guards_sharing_bot_state_send_one_page() -> None:
    """The shared throttle makes parallel detectors a single page."""
    bot, pool = _FakeBot(), _FakePool()

    async def _page() -> None:
        await entrypoint.maybe_notify_redis_outage(
            bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
        )

    redis1, redis2 = _FakeRedis(), _FakeRedis()
    guard1, guard2 = await _single_guard(redis1), await _single_guard(redis2)
    redis1.down = redis2.down = True
    for tick in range(5):
        await guard1.refresh_once(on_redis_down=_page, now=T0 + 20.0 * tick)
        await guard2.refresh_once(on_redis_down=_page, now=T0 + 20.0 * tick)

    assert len(bot.sent) == 2, "two detectors, one shared page"


async def test_the_window_expiring_pages_again() -> None:
    bot, pool = _FakeBot(), _FakePool()

    assert await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    ) is True
    assert await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW + timedelta(hours=6, seconds=1)  # type: ignore[arg-type]
    ) is True

    assert len(bot.sent) == 4


async def test_postgres_down_is_absorbed_not_raised() -> None:
    bot, pool = _FakeBot(), _FailingPool()

    sent = await entrypoint.maybe_notify_redis_outage(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )

    assert sent is False
    assert bot.sent == [], "no page without the throttle to dedupe it"
