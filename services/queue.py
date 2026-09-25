"""Async task queue: Redis-backed by default, in-memory fallback for dev/tests.

The Gateway (bot) only pushes tasks here; background workers pop and process
them, so the bot process never does heavy work.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Optional

import redis.asyncio as aioredis

from core.config import Settings
from core.utils import MediaFormat

logger = logging.getLogger(__name__)

#: How long a worker blocks on a queue read before re-checking the stop event.
#: Kept short so Ctrl+C/SIGTERM shuts the bot down in seconds instead of
#: waiting out a long block, without hammering Redis while idle.
BLOCK_TIMEOUT_S = 5

#: redis-py's connection default is a 5s *socket* timeout. A blocking BRPOP that
#: waits longer than that gets aborted by the client before the server's own
#: timer fires, raising ``TimeoutError`` instead of returning None (and, with a
#: naive worker loop, killing the worker). Always keep the socket window wider
#: than the block timeout — see ``create_redis_client``.
REDIS_SOCKET_TIMEOUT_S = BLOCK_TIMEOUT_S + 10

#: How long a single-flight claim outlives its job before a crashed worker stops
#: blocking the same request. Every job releases its claim the moment it settles
#: (delivered or finally failed); this TTL is only the safety net for a worker
#: that died mid-download and never reported back. Generous on purpose — a slow
#: 600 MB fetch on a thin link is not a deadlock — and bounded so no claim can
#: live forever.
JOB_LOCK_TTL_S = 2 * 60 * 60


def job_key(task: "DownloadTask") -> str:
    """The single-flight name of one job: one user, one URL, one request.

    Two triggers for the same key are the same download twice — the queue
    accepts the first and refuses every repeat until that job settles. Other
    users (and other tiers of the same link) are different jobs and never block
    each other. ``url_hash`` already folds URL + request together; the raw fields
    are the fallback for older payloads that never carried one.
    """
    return f"{task.telegram_id}|{task.url_hash or task.url}|{task.media_format}|{task.quality}"


class _JobClaims:
    """The in-memory claim store (non-Redis backends).

    Check-and-set is one synchronous step — nothing awaits between reading and
    writing — so two concurrent triggers can never both win the same key.
    """

    def __init__(self) -> None:
        self._held: dict[str, float] = {}

    def try_claim(self, key: str, ttl: float, now: float) -> bool:
        self._held = {name: until for name, until in self._held.items() if until > now}
        if self._held.get(key, 0.0) > now:
            return False
        self._held[key] = now + ttl
        return True

    def release(self, key: str) -> None:
        self._held.pop(key, None)


def create_redis_client(url: str) -> aioredis.Redis:
    """Redis client tuned for blocking queue reads and FSM storage."""
    return aioredis.from_url(
        url,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT_S,
    )


@dataclass(slots=True)
class DownloadTask:
    """One queued download request (serialized as JSON in the queue).

    It carries two things beyond the link, and both exist because the worker is a
    different process from the update that started it: ``quality`` (the tier the
    user picked — a height ceiling, or which audio format) and ``lang`` (so the
    background messages arrive in the language the user reads, long after the
    ``Message`` object is gone).
    """

    url: str
    telegram_id: int
    chat_id: int
    media_format: MediaFormat = "video"
    quality: str = ""
    lang: str = ""
    url_hash: str = ""
    platform: str = ""
    attempts: int = 0
    #: Two UI facts ride along for the worker's narration: the display title the
    #: gateway already probed, and the message whose text *is* this job's media
    #: card. The worker edits that message instead of opening a "processing…"
    #: one of its own — the tap, the wait and the result stay one screen. ``0``
    #: means "no card message" (an older payload, a direct call), and the worker
    #: opens one then.
    title: str = ""
    status_message_id: int = 0
    #: The group's display name when the request came from one (often unknown on
    #: older payloads — analytics readers must survive the empty string and fall
    #: back to the chat id).
    chat_title: str = ""

    #: The question-time probe's verdicts — ``None`` when nothing probed (older
    #: payloads), in which case the worker checks for itself. They travel with
    #: the task so one job costs one extraction, not two.
    is_live: Optional[bool] = None
    size_estimate: Optional[int] = None

    def to_payload(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_payload(cls, raw: str) -> "DownloadTask":
        data: dict[str, Any] = json.loads(raw)
        fields = cls.__dataclass_fields__
        return cls(**{key: data[key] for key in fields if key in data})


class TaskQueue(ABC):
    """Minimal queue contract: push (single-flight), pop, release, depth."""

    @abstractmethod
    async def enqueue(self, task: DownloadTask) -> int:
        """Push a task; returns the approximate queue depth after the push.

        Returns ``-1`` instead when this exact job is *already* in flight (see
        :func:`job_key`): the duplicate trigger is refused so one request can
        never produce two downloads — or two deliveries of the same file.
        """

    @abstractmethod
    async def release(self, task: DownloadTask) -> None:
        """Settle the job's single-flight claim so the request may run again.

        Called by the worker the moment the job is truly done — delivered or
        finally failed. Must never raise: the claim's TTL is the safety net, and
        a release hiccup must not turn a finished job into a second one.
        """

    @abstractmethod
    async def dequeue(self) -> Optional[DownloadTask]:
        """Block up to ``BLOCK_TIMEOUT_S`` for the next task; None on timeout."""

    @abstractmethod
    async def requeue(self, task: DownloadTask) -> int:
        """Put a task back at the head of the queue; returns the new depth.

        Used when a worker is interrupted mid-flight (shutdown, retry) so the
        request isn't silently lost.
        """

    @abstractmethod
    async def depth(self) -> int:
        """Current number of pending tasks."""

    async def close(self) -> None:  # noqa: B027 — shared clients close upstream
        return None


class RedisTaskQueue(TaskQueue):
    """Redis list-based queue (LPUSH producers / BRPOP workers).

    The single-flight claim is a ``SET NX EX`` — Redis' own atomic check-and-set,
    so the lock holds across processes and replicas: the gateway and every worker
    see the same claims.
    """

    def __init__(self, redis: Any, name: str) -> None:
        self.redis = redis
        self.name = name

    def _claim_name(self, task: DownloadTask) -> str:
        return f"{self.name}:job:{job_key(task)}"

    async def enqueue(self, task: DownloadTask) -> int:
        fresh = await self.redis.set(self._claim_name(task), "1", nx=True, ex=JOB_LOCK_TTL_S)
        if not fresh:
            return -1
        try:
            await self.redis.lpush(self.name, task.to_payload())
        except Exception:
            await self.release(task)  # no push, no job — the claim must not linger
            raise
        return int(await self.redis.llen(self.name))

    async def release(self, task: DownloadTask) -> None:
        try:
            await self.redis.delete(self._claim_name(task))
        except Exception:
            logger.debug("could not release the claim for %s", task.url, exc_info=True)

    async def dequeue(self) -> Optional[DownloadTask]:
        item = await self.redis.brpop(self.name, timeout=BLOCK_TIMEOUT_S)
        if item is None:
            return None
        _, payload = item
        return DownloadTask.from_payload(payload)

    async def requeue(self, task: DownloadTask) -> int:
        # Producers LPUSH, consumers BRPOP (FIFO) — so a requeue goes to the
        # right end to be picked up next instead of after the whole backlog.
        await self.redis.rpush(self.name, task.to_payload())
        return await self.depth()

    async def depth(self) -> int:
        return int(await self.redis.llen(self.name) or 0)


class MemoryTaskQueue(TaskQueue):
    """In-process queue — for local development and tests only."""

    def __init__(self) -> None:
        self._items: asyncio.Queue[DownloadTask] = asyncio.Queue()
        self._claims = _JobClaims()

    async def enqueue(self, task: DownloadTask) -> int:
        if not self._claims.try_claim(job_key(task), JOB_LOCK_TTL_S, time.monotonic()):
            return -1
        await self._items.put(task)
        return self._items.qsize()

    async def release(self, task: DownloadTask) -> None:
        self._claims.release(job_key(task))

    async def dequeue(self) -> Optional[DownloadTask]:
        try:
            return await asyncio.wait_for(self._items.get(), timeout=BLOCK_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None

    async def requeue(self, task: DownloadTask) -> int:
        await self._items.put(task)
        return self._items.qsize()

    async def depth(self) -> int:
        return self._items.qsize()


def create_queue(settings: Settings, redis: Any | None) -> TaskQueue:
    """Pick the queue backend: Redis when available, else in-memory."""
    if settings.queue_backend == "memory" or redis is None:
        if settings.queue_backend == "redis" and redis is None:
            logger.warning("Redis unavailable — using in-memory queue (tasks lost on restart).")
        return MemoryTaskQueue()
    return RedisTaskQueue(redis, settings.queue_name)
