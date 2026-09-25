"""Queue tests: task serialization, FIFO ordering, requeue and backend selection.

No Redis needed — ``RedisTaskQueue`` is exercised against a tiny fake that
mirrors the LPUSH/BRPOP/RPUSH semantics the real client provides.
"""

from __future__ import annotations

import pytest

from core.config import get_settings
from core.utils import MediaFormat
from services import queue as queue_module
from services.queue import (
    BLOCK_TIMEOUT_S,
    REDIS_SOCKET_TIMEOUT_S,
    DownloadTask,
    MemoryTaskQueue,
    RedisTaskQueue,
    create_queue,
    create_redis_client,
)


class FakeRedis:
    """Just enough Redis for the queue: a list (LPUSH/BRPOP/RPUSH/LLEN) and the
    key/value pair the single-flight claim rides on (SET NX / DELETE)."""

    def __init__(self) -> None:
        self.items: list[str] = []
        self.kv: dict[str, str] = {}

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> str | None:
        if nx and name in self.kv:
            return None
        self.kv[name] = value
        return "OK"

    async def delete(self, name: str) -> int:
        return 1 if self.kv.pop(name, None) is not None else 0

    async def lpush(self, _name: str, value: str) -> int:
        self.items.insert(0, value)
        return len(self.items)

    async def rpush(self, _name: str, value: str) -> int:
        self.items.append(value)
        return len(self.items)

    async def brpop(self, name: str, timeout: int = 0) -> tuple[str, str] | None:
        if not self.items:
            return None
        return name, self.items.pop()

    async def llen(self, _name: str) -> int:
        return len(self.items)


def _task(url: str = "https://youtu.be/abc", media_format: MediaFormat = "video") -> DownloadTask:
    return DownloadTask(
        url=url,
        telegram_id=424242,
        chat_id=424242,
        media_format=media_format,
        # A stand-in for the real sha256 cache key — distinct per URL, so two
        # different links are two different jobs to the single-flight lock.
        url_hash=f"hash-of:{url}",
    )


def test_task_payload_roundtrip() -> None:
    task = _task(media_format="audio")
    assert DownloadTask.from_payload(task.to_payload()) == task


def test_task_payload_ignores_unknown_fields() -> None:
    payload = '{"url": "https://x/1", "telegram_id": 1, "chat_id": 1, "future_field": 42}'
    task = DownloadTask.from_payload(payload)
    assert task.url == "https://x/1" and task.media_format == "video"


async def test_memory_queue_is_fifo() -> None:
    queue = MemoryTaskQueue()
    first, second = _task("https://x/1"), _task("https://x/2")
    assert await queue.enqueue(first) == 1
    assert await queue.enqueue(second) == 2
    assert await queue.depth() == 2
    assert await queue.dequeue() == first
    assert await queue.dequeue() == second


async def test_memory_requeue_returns_it_to_the_front() -> None:
    queue = MemoryTaskQueue()
    task = _task()
    await queue.enqueue(task)
    assert await queue.dequeue() == task
    assert await queue.requeue(task) == 1
    assert await queue.depth() == 1
    assert await queue.dequeue() == task


async def test_redis_queue_is_fifo_and_supports_requeue() -> None:
    queue = RedisTaskQueue(FakeRedis(), "test:queue")
    first, second = _task("https://x/1"), _task("https://x/2")
    await queue.enqueue(first)
    await queue.enqueue(second)
    assert await queue.depth() == 2
    assert await queue.dequeue() == first
    assert await queue.requeue(first) == 2
    assert await queue.dequeue() == first  # requeued work goes next
    assert await queue.dequeue() == second


async def test_redis_dequeue_returns_none_when_empty() -> None:
    assert await RedisTaskQueue(FakeRedis(), "test:queue").dequeue() is None


def test_block_timeout_stays_snappy_for_shutdown() -> None:
    # A long block here would make Ctrl+C/SIGTERM hang for that long.
    assert 0 < BLOCK_TIMEOUT_S <= 10


def test_redis_socket_timeout_is_wider_than_the_block_timeout() -> None:
    assert REDIS_SOCKET_TIMEOUT_S > BLOCK_TIMEOUT_S


def test_redis_client_sets_an_explicit_socket_timeout() -> None:
    """Regression: redis-py's absent-key default (5s) aborts a blocking BRPOP.

    The socket timeout must be passed explicitly and be wider than the block
    timeout, otherwise an idle worker sees ``TimeoutError`` instead of None.
    """
    kwargs = create_redis_client("redis://localhost:6379/0").get_connection_kwargs()
    assert "socket_timeout" in kwargs
    assert kwargs["socket_timeout"] == REDIS_SOCKET_TIMEOUT_S
    assert kwargs["socket_timeout"] > BLOCK_TIMEOUT_S


async def test_memory_dequeue_returns_none_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(queue_module, "BLOCK_TIMEOUT_S", 0.05)
    assert await MemoryTaskQueue().dequeue() is None


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_create_queue_without_redis_falls_back_to_memory(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    monkeypatch.setenv("QUEUE_BACKEND", backend)
    get_settings.cache_clear()
    try:
        assert isinstance(create_queue(get_settings(), None), MemoryTaskQueue)
    finally:
        get_settings.cache_clear()


def test_create_queue_uses_redis_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUEUE_BACKEND", "redis")
    get_settings.cache_clear()
    try:
        queue = create_queue(get_settings(), FakeRedis())
        assert isinstance(queue, RedisTaskQueue)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Single-flight: one job, one task — no matter how often it is asked for
# ---------------------------------------------------------------------------


async def test_one_job_is_one_task_while_it_is_in_flight() -> None:
    """The duplicate-delivery bug, cut at its root: the same job is refused
    while it flies and only a *settled* job may run again."""
    queue = MemoryTaskQueue()
    task = _task()
    assert await queue.enqueue(task) == 1
    assert await queue.enqueue(_task()) == -1, "the same job is already flying"
    assert await queue.depth() == 1
    await queue.release(task)
    assert await queue.enqueue(_task()) == 2, "settled — the request may run again"


async def test_other_requests_and_other_users_are_other_jobs() -> None:
    """Single-flight is per user and per request — it never blocks a different
    ask, and never blocks another person's copy of the same file."""
    queue = MemoryTaskQueue()
    await queue.enqueue(_task())
    assert await queue.enqueue(_task(media_format="audio")) == 2
    other_user = DownloadTask(
        url="https://youtu.be/abc", telegram_id=1, chat_id=1, url_hash="hash-of:other"
    )
    assert await queue.enqueue(other_user) == 3


async def test_the_claim_is_shared_between_queue_clients() -> None:
    """The claim is ``SET NX`` — the lock every process sees, so a second gateway
    (or a second worker) refuses the very same job."""
    fake = FakeRedis()
    first, second = RedisTaskQueue(fake, "test:queue"), RedisTaskQueue(fake, "test:queue")
    task = _task()
    assert await first.enqueue(task) == 1
    assert await second.enqueue(_task()) == -1
    await first.release(task)
    assert await second.enqueue(_task()) == 2


def test_a_claim_nobody_released_expires() -> None:
    """A worker that died mid-download must not lock the request forever — the
    TTL is the safety net every claim bottoms out at."""
    claims = queue_module._JobClaims()
    assert claims.try_claim("job", ttl=10.0, now=100.0)
    assert not claims.try_claim("job", ttl=10.0, now=105.0), "held — refused"
    assert claims.try_claim("job", ttl=10.0, now=110.0), "expired — the net worked"
