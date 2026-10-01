"""Single-instance tripwire: warn when two live bot processes share one Redis.

Session modes, the double-tap guard and the single-flight claim shelf are all
per-process. One bot process per Redis is therefore a correctness requirement,
not a scaling preference — and nothing enforces it: a second ``docker compose
up``, a split gateway/worker topology, or a host-run bot next to the compose
one all fork that state silently. This module is the tripwire, not the fix
(redesigning the coordination layer is explicitly out of scope): a best-effort
Redis lease (unique id, TTL, periodic refresh, released on graceful shutdown).
A live foreign lease means log loudly + surface in /doctor. Default is warn,
never fail; the guard itself must never block a boot.

What it detects: two live processes sharing one Redis (one lease key, a foreign
id refreshed recently). What it does not: anything about Telegram — two polling
instances with the same token already get conflict errors from Telegram itself,
so the guard matters most for webhook mode, split gateway/worker, and a host
bot sharing Redis with the compose stack. It also cannot tell a *crashed* peer
from a live one at first sight (a dead lease looks live until its TTL runs
down) — hence the grace period plus re-check before warning, and periodic
re-evaluation that clears a transient warning once the dead lease goes stale.

All state lives on the instance. Module scope holds only immutable constants —
no shared mutable coordination state is added by this file.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import redis.exceptions as _redis_errors

from services.doctor import Check

logger = logging.getLogger(__name__)

#: The one lease every instance contends for: holder refreshes it, an observer
#: only reads it. Never names a user, a chat or a link — id plus epoch.
LEASE_KEY = "bot:instance_lease"
#: How long a lease outlives a holder that stopped refreshing (a crash, a kill).
#: An ``int`` on purpose: it is passed as the ``EX`` argument of ``SET``, and
#: redis-py raises ``DataError`` on anything but ``int``/``timedelta`` (a float
#: here once meant no lease was ever written while every test stayed green).
LEASE_TTL_S = 60
#: How often the holder refreshes while alive.
REFRESH_S = 20.0
#: How long a fresh start waits before judging a foreign lease it found: a
#: restart-after-crash meets its own dead lease here, and judging it live
#: would page on every unlucky restart.
STARTUP_GRACE_S = 5.0
#: A lease this close to expiry was not refreshed lately: its holder is gone
#: (or dying), so taking over is safe and silent. Above it the holder is alive
#: enough to deserve the warning instead.
STALE_TTL_S = 30.0
#: How long the lease refresh must fail continuously before the outage is
#: paged (see ``refresh_once``): a restart or a blip stays silent, a dead
#: Redis does not. Ticks arrive every REFRESH_S, so three bad ticks page.
REDIS_DOWN_ALERT_AFTER_S = 60.0


def _is_unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` means Redis itself is unreachable (as opposed to a bug).

    Only these feed the outage streak: anything else (a bad argument, a parse
    failure, a programming error) degrades the guard without ever paging about
    a Redis outage that does not exist.
    """
    return isinstance(
        exc,
        (
            _redis_errors.ConnectionError,
            _redis_errors.TimeoutError,
            TimeoutError,
            OSError,
        ),
    )


def _short(message: object, limit: int = 120) -> str:
    """One log-safe line: truncated, no newlines."""
    return " ".join(str(message).split())[:limit]


class InstanceGuard:
    """One process's view of the lease: holder (``single``) or observer."""

    def __init__(self, redis: Any | None) -> None:
        self._redis = redis
        self.instance_id = uuid.uuid4().hex[:12] if redis is not None else ""
        self.started_at = time.time()
        #: ``single`` (holder, alone), ``duplicate`` (live foreign holder seen),
        #: ``disabled`` (no Redis — memory mode is single-process by definition),
        #: ``unknown`` (the guard itself failed — never blocks anything).
        self.state = "disabled" if redis is None else "unknown"
        self.peer_id = ""
        #: When the current refresh-failure streak started (monotonic), or
        #: ``None`` while the last refresh answered. Instance state, never
        #: module state: a restart begins unaccused, as it should.
        self._redis_down_since: float | None = None
        #: Whether the current degraded spell was already announced: the ONE
        #: warning per spell lives here (instance state, never module state).
        #: Cleared on recovery, so the next spell warns again.
        self._degraded_announced = False

    @staticmethod
    def _lease_value(instance_id: str, started_at: float) -> str:
        """The lease as stored: id plus boot epoch, nothing secret or personal."""
        return f"{instance_id}|{started_at:.0f}"

    @staticmethod
    def _parse_lease(value: object) -> tuple[str, float] | None:
        """A stored lease back into ``(id, epoch)`` — ``None`` when not ours."""
        if not isinstance(value, str):
            return None
        head, sep, tail = value.partition("|")
        if not sep or not head:
            return None
        try:
            return head, float(tail)
        except ValueError:
            return None

    def _own_value(self) -> str:
        return self._lease_value(self.instance_id, self.started_at)

    def _client(self) -> Any:
        """The Redis client — present everywhere this is called (checked first)."""
        assert self._redis is not None
        return self._redis

    async def _read(self) -> tuple[str, float, float] | None:
        """The live lease as ``(id, epoch, ttl)`` — ``None`` when no lease."""
        raw = await self._client().get(LEASE_KEY)
        parsed = self._parse_lease(raw)
        if parsed is None:
            return None
        instance_id, started = parsed
        try:
            ttl = float(await self._client().ttl(LEASE_KEY))
        except Exception:
            ttl = -2.0
        return instance_id, started, ttl

    async def _claim(self) -> bool:
        """Take an absent lease; ``False`` when someone else holds it."""
        return bool(
            await self._client().set(LEASE_KEY, self._own_value(), nx=True, ex=LEASE_TTL_S)
        )

    async def _takeover(self) -> None:
        """Replace a stale lease outright — its holder is gone by definition."""
        await self._client().set(LEASE_KEY, self._own_value(), ex=LEASE_TTL_S)

    async def _refresh_holder(self) -> bool:
        """Renew our own lease; ``False`` when it is no longer ours."""
        current = await self._read()
        if current is None or current[0] != self.instance_id:
            return False
        await self._client().set(LEASE_KEY, self._own_value(), ex=LEASE_TTL_S)
        return True

    def _enter(self, state: str, peer_id: str = "", *, error: BaseException | None = None) -> None:
        """Move state, logging only the transitions an operator must see.

        The first failure-driven entry into ``unknown``/``disabled`` logs ONE
        warning (exception class + short message — never a URL or credential:
        Redis errors name at most host:port); the rest of the spell stays
        silent. Recovery to ``single`` logs one info line and re-arms the
        warning for the next spell. Deliberate states (a healthy claim, a
        live peer, memory mode with no error) keep their existing lines.
        """
        if state in ("unknown", "disabled") and error is not None:
            if not self._degraded_announced:
                logger.warning(
                    "instance-lease %s (%s: %s)",
                    state,
                    type(error).__name__,
                    _short(error),
                )
                self._degraded_announced = True
        elif state == "single" and self._degraded_announced:
            logger.info(
                "instance-lease ok: this process holds the lease (%s)",
                self.instance_id,
            )
            self._degraded_announced = False
        if self.state != "duplicate" and state == "duplicate":
            logger.warning(
                "another live bot instance shares this Redis (peer %s) — "
                "session modes and single-flight claims are now forked; "
                "keep exactly one instance",
                peer_id,
            )
        elif self.state == "duplicate" and state == "single":
            logger.info(
                "the foreign instance lease went stale — this process holds "
                "the lease again (peer %s is gone)",
                self.peer_id,
            )
        self.state = state
        self.peer_id = peer_id

    async def _observe(self) -> None:
        """One look at the lease, then the matching state — never raises."""
        current = await self._read()
        if current is None:
            if await self._claim():
                self._enter("single")
            else:
                # Lost the race between the read and the claim: look again
                # next time instead of guessing.
                await self._observe_foreign()
            return
        instance_id, _started, ttl = current
        if instance_id == self.instance_id:
            self._enter("single")
        elif ttl < 0 or ttl <= STALE_TTL_S:
            # Absent TTL (-2/-1 raced past expiry, or a lease with no expiry
            # from an older writer) or untouched for a while: a dead peer's
            # lease, taken over silently.
            await self._takeover()
            self._enter("single")
        else:
            self._enter("duplicate", peer_id=instance_id)

    async def _observe_foreign(self) -> None:
        """The claim above lost its race: record whoever won, without writing."""
        try:
            current = await self._read()
        except Exception as exc:
            logger.debug("instance-lease re-read failed", exc_info=True)
            self._enter("unknown", error=exc)
            return
        if current is None or current[0] == self.instance_id:
            self._enter("single")
        else:
            self._enter("duplicate", peer_id=current[0])

    async def start(self, *, grace_s: float = STARTUP_GRACE_S) -> "InstanceGuard":
        """Claim or observe the lease; never raises, never blocks long.

        A foreign lease found at boot gets a grace period plus a re-check
        before any warning: that is exactly what a restart-after-crash looks
        like, and paging on it would cry wolf on every unlucky deploy.
        """
        if self._redis is None:
            self._enter("disabled")
            return self
        try:
            current = await self._read()
            if current is None or current[0] == self.instance_id:
                if await self._claim():
                    self._enter("single")
                    return self
            if grace_s > 0:
                await asyncio.sleep(grace_s)
            await self._observe()
        except Exception as exc:
            logger.debug("instance-lease check failed — continuing unguarded", exc_info=True)
            self._enter("unknown", error=exc)
        return self

    def _note_redis_failure(self, now: float) -> bool:
        """Record a failed refresh; True while the failure is sustained.

        Level-triggered, not edge: True on every tick once the streak has
        lasted ``REDIS_DOWN_ALERT_AFTER_S``. The shared bot_state throttle
        (not this streak) is what makes sustained paging a single notice —
        so a page lost to a Postgres outage is retried on the next tick
        instead of being swallowed with the edge. Only reachability failures
        may start this streak (see ``refresh_once``): a programming error
        must never page about a Redis outage.
        """
        if self._redis_down_since is None:
            self._redis_down_since = now
        return now - self._redis_down_since >= REDIS_DOWN_ALERT_AFTER_S

    async def refresh_once(
        self,
        *,
        on_redis_down: Callable[[], Awaitable[None]] | None = None,
        now: float | None = None,
    ) -> None:
        """One periodic re-evaluation: renew our lease, or re-judge a peer's.

        This is what clears a transient duplicate: a crashed peer's lease goes
        stale within one TTL, and the next tick takes it over silently instead
        of warning forever. Never raises.

        When the refresh keeps failing, ``on_redis_down`` is awaited once the
        outage is sustained (see ``REDIS_DOWN_ALERT_AFTER_S``) — the one admin
        page for a mid-run Redis death. Memory mode (no Redis) never pages:
        there is no lease to refresh and nothing to detect. ``now`` is the
        monotonic clock, injectable for tests.
        """
        moment = time.monotonic() if now is None else now
        if self._redis is None:
            self._enter("disabled")
            self._redis_down_since = None
            return
        try:
            if self.state == "single":
                if not await self._refresh_holder():
                    await self._observe()
            else:
                await self._observe()
        except Exception as exc:
            logger.debug("instance-lease refresh failed", exc_info=True)
            self._enter("unknown", error=exc)
            if (
                _is_unreachable(exc)
                and self._note_redis_failure(moment)
                and on_redis_down is not None
            ):
                try:
                    await on_redis_down()
                except Exception:
                    logger.debug("redis-outage notice failed — retrying next tick", exc_info=True)
            return
        self._redis_down_since = None

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        on_redis_down: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """The refresh loop, owned like the worker tasks; exits on stop."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=REFRESH_S)
                return
            except asyncio.TimeoutError:
                pass
            await self.refresh_once(on_redis_down=on_redis_down)

    async def stop(self) -> None:
        """Release our own lease on graceful shutdown; never raises, never blocks."""
        if self._redis is None:
            return
        try:
            current = await self._read()
            if current is not None and current[0] == self.instance_id:
                await self._client().delete(LEASE_KEY)
        except Exception:
            logger.debug("instance-lease release failed", exc_info=True)

    def as_check(self) -> Check:
        """This process's instance row for /doctor — warn-only by design."""
        if self.state == "single":
            return Check(
                "تک‌نمونه",
                "ok",
                f"این نمونه lease را دارد ({self.instance_id})؛ نمونهٔ دیگری دیده نشد",
                section=True,
            )
        if self.state == "duplicate":
            return Check(
                "تک‌نمونه",
                "warn",
                f"نمونهٔ زندهٔ دیگری دیده شد ({self.peer_id}): حالت دانلود و قفل‌ها "
                "بین دو فرایند تقسیم شده‌اند — فقط یکی را نگه دارید",
                section=True,
            )
        if self.state == "disabled":
            return Check(
                "تک‌نمونه",
                "ok",
                "بدون Redis (حالت حافظه): تک‌فرایندی بودن تضمینی است",
                section=True,
            )
        return Check(
            "تک‌نمونه",
            "warn",
            "وضعیت نمونه‌ها مشخص نشد (Redis در دسترس نبود یا خطا داد)",
            section=True,
        )


async def start_guard(
    redis: Any | None, *, grace_s: float = STARTUP_GRACE_S
) -> InstanceGuard:
    """Build and start the tripwire; never raises, never blocks the boot.

    Double safety on top of :meth:`InstanceGuard.start` (which already absorbs
    its own failures): even an unexpected error here degrades to an
    ``unknown``-state guard instead of a failed startup.
    """
    guard = InstanceGuard(redis)
    try:
        await guard.start(grace_s=grace_s)
    except Exception:
        logger.debug("instance guard failed to start — continuing unguarded", exc_info=True)
        guard.state = "unknown"
    return guard
