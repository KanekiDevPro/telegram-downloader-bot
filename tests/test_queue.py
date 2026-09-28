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
    """Just enough Redis for the queue: named lists (LPUSH/BRPOP/RPUSH/RPOP/
    LREM/LLEN) and the key/value pair the single-flight claim rides on
    (SET NX / DELETE)."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.kv: dict[str, str] = {}

    def _list(self, name: str) -> list[str]:
        return self.lists.setdefault(name, [])

    async def set(
        self, name: str, value: str, nx: bool = False, ex: int | None = None
    ) -> str | None:
        if nx and name in self.kv:
            return None
        self.kv[name] = value
        return "OK"

    async def delete(self, name: str) -> int:
        return 1 if self.kv.pop(name, None) is not None else 0

    async def lpush(self, name: str, value: str) -> int:
        items = self._list(name)
        items.insert(0, value)
        return len(items)

    async def rpush(self, name: str, value: str) -> int:
        items = self._list(name)
        items.append(value)
        return len(items)

    async def brpop(self, name: str, timeout: int = 0) -> tuple[str, str] | None:
        items = self._list(name)
        if not items:
            return None
        return name, items.pop()

    async def rpop(self, name: str) -> str | None:
        items = self._list(name)
        return items.pop() if items else None

    async def lrem(self, name: str, count: int, value: str) -> int:
        items = self._list(name)
        if value in items:
            items.remove(value)
            return 1
        return 0

    async def llen(self, name: str) -> int:
        return len(self._list(name))


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


# ---------------------------------------------------------------------------
# Durability: a worker that dies must not take the job with it
# ---------------------------------------------------------------------------


async def test_a_task_whose_worker_dies_is_processed_by_the_next_one() -> None:
    """P1.5, pinned: the pop itself used to be the loss. ``brpop`` removed the
    payload, so kill -9, an OOM or a container restart mid-download destroyed
    the job and the user's card sat on ⏳ forever. The two-step pop shelves the
    raw item the moment it leaves the queue; a new worker's ``requeue_orphans``
    brings every orphan home — and a settled task leaves the shelf for good."""
    redis = FakeRedis()
    first = RedisTaskQueue(redis, "test:queue")
    task = _task()
    await first.enqueue(task)

    popped = await first.dequeue()
    assert popped == task
    # ...and the worker dies here: no release, no requeue, nothing at all.
    second = RedisTaskQueue(redis, "test:queue")
    assert await second.requeue_orphans() == 1, "the orphan is found on the shelf"
    assert await second.dequeue() == task, "and processed after all"
    await second.release(task)
    assert await second.depth() == 0
    assert redis._list("test:queue:processing") == [], "a settled task leaves the shelf"


async def test_a_settled_task_never_runs_twice_from_the_shelf() -> None:
    """The shelf is emptied by finishing, not only by recovery: a job that ran
    to its end is not an orphan and must never be handed out again."""
    redis = FakeRedis()
    queue = RedisTaskQueue(redis, "test:queue")
    task = _task()
    await queue.enqueue(task)
    await queue.dequeue()
    await queue.release(task)

    assert await queue.requeue_orphans() == 0, "a finished job is not an orphan"
    assert await queue.dequeue() is None


async def test_a_payload_that_cannot_parse_is_kept_recoverable() -> None:
    """A shelf, not a shredder: an item that will not parse stays parked for
    the next ``requeue_orphans`` instead of being silently destroyed."""
    redis = FakeRedis()
    queue = RedisTaskQueue(redis, "test:queue")
    await redis.rpush("test:queue", "{not json")

    with pytest.raises(ValueError):
        await queue.dequeue()
    assert await queue.requeue_orphans() == 1, "parked, not destroyed"


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


def test_the_claim_ttl_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TTL is deployment policy, not a constant: the queue reads the setting
    at claim time (the two-hour default lives in Settings)."""
    monkeypatch.setenv("JOB_LOCK_TTL_S", "120")
    get_settings.cache_clear()
    try:
        assert queue_module.claim_ttl() == 120
    finally:
        get_settings.cache_clear()
