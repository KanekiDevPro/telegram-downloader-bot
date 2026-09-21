"""Async task queue: Redis-backed by default, in-memory fallback for dev/tests.

The Gateway (bot) only pushes tasks here; background workers pop and process
them, so the bot process never does heavy work.
"""

from __future__ import annotations

import asyncio
import json
import logging
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


def create_redis_client(url: str) -> aioredis.Redis:
    """Redis client tuned for blocking queue reads and FSM storage."""
    return aioredis.from_url(
        url,
        decode_responses=True,
        socket_timeout=REDIS_SOCKET_TIMEOUT_S,
    )


@dataclass(slots=True)
class DownloadTask:
    """One queued download request (serialized as JSON in the queue)."""

    url: str
    telegram_id: int
    chat_id: int
    media_format: MediaFormat = "video"
    url_hash: str = ""
    platform: str = ""
    attempts: int = 0

    def to_payload(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_payload(cls, raw: str) -> "DownloadTask":
        data: dict[str, Any] = json.loads(raw)
        fields = cls.__dataclass_fields__
        return cls(**{key: data[key] for key in fields if key in data})


class TaskQueue(ABC):
    """Minimal queue contract: push, pop (blocking), depth."""

    @abstractmethod
    async def enqueue(self, task: DownloadTask) -> int:
        """Push a task; returns the approximate queue depth after the push."""

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
    """Redis list-based queue (LPUSH producers / BRPOP workers)."""

    def __init__(self, redis: Any, name: str) -> None:
        self.redis = redis
        self.name = name

    async def enqueue(self, task: DownloadTask) -> int:
        await self.redis.lpush(self.name, task.to_payload())
        return int(await self.redis.llen(self.name))

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

    async def enqueue(self, task: DownloadTask) -> int:
        await self._items.put(task)
        return self._items.qsize()

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
