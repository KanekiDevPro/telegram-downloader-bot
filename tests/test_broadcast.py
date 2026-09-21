"""The global broadcast: who it reaches, what it counts, and how it behaves.

A broadcast is the one panel action a mistake cannot be taken back from, so what is
pinned here is the accounting rather than the sending: every id in the database is
visited exactly once (paged, so the memory cost does not grow with the user count),
a user who blocked the bot is *counted* rather than treated as an error, Telegram's
own rate limit is obeyed rather than fought, and one failure never stops the run.

Nothing here talks to Telegram: the bot is a recorder, and the pages come from a
list.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from services import broadcast


class RecordingBot:
    """Answers per recipient, and remembers what was sent to whom."""

    def __init__(self, outcomes: dict[int, list[Any]] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        #: id → the exceptions to raise, in order (an empty list = deliver).
        self._outcomes = {key: list(value) for key, value in (outcomes or {}).items()}

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        queue = self._outcomes.get(chat_id) or []
        if queue:
            # Each queued outcome is raised once, in order — so a second call for
            # the same recipient (the retry after a rate limit) delivers unless a
            # second refusal was queued for it deliberately.
            raise queue.pop(0)
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


def _blocked() -> Any:
    """Telegram's 403, raised the way the API raises it (the ``method`` is not
    None in practice — a fake only has no request object to name)."""
    return TelegramForbiddenError(method=None, message="bot was blocked")  # type: ignore[arg-type]


def _rate_limited(seconds: int) -> Any:
    """A 429 carrying the API's own ``retry_after``."""
    message = f"Too Many Requests: retry after {seconds}"
    return TelegramRetryAfter(method=None, message=message, retry_after=seconds)  # type: ignore[arg-type]


def _bad_request() -> Any:
    return TelegramBadRequest(method=None, message="chat not found")  # type: ignore[arg-type]


def _pages(ids: list[int]) -> Any:
    """``user_id_page`` over a plain list (keyset paging, like the real query)."""

    async def user_id_page(pool: Any, after: int = 0, limit: int = 500) -> list[int]:
        remaining = [value for value in ids if value > after]
        return remaining[:limit]

    return user_id_page


@pytest.fixture(autouse=True)
def _no_real_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """``asyncio.sleep`` records instead of waiting (the pacing is asserted)."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(broadcast.asyncio, "sleep", sleep)
    return slept


def _install(monkeypatch: pytest.MonkeyPatch, ids: list[int]) -> None:
    monkeypatch.setattr(broadcast.database, "user_id_page", _pages(ids))


async def test_every_user_gets_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [10, 11, 12])
    bot = RecordingBot()

    report = await broadcast.deliver(bot, object(), "hello", interval_s=0)  # type: ignore[arg-type]

    assert bot.sent == [(10, "hello"), (11, "hello"), (12, "hello")]
    assert (report.total, report.sent, report.blocked, report.failed) == (3, 3, 0, 0)
    assert report.ok


async def test_the_pages_are_walked_rather_than_the_table_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One query per page, keyed past the last id — the run's memory is one page."""
    ids = list(range(1, 7))
    calls: list[tuple[int, int]] = []

    async def user_id_page(pool: Any, after: int = 0, limit: int = 500) -> list[int]:
        calls.append((after, limit))
        return [value for value in ids if value > after][:limit]

    monkeypatch.setattr(broadcast.database, "user_id_page", user_id_page)
    reached: list[int] = []

    async def send_message(chat_id: int, text: str, **kwargs: Any) -> Any:
        reached.append(chat_id)
        return SimpleNamespace(message_id=1)

    bot = SimpleNamespace(send_message=send_message)
    await broadcast.deliver(bot, object(), "hi", interval_s=0, page_size=3)  # type: ignore[arg-type]

    # Three requests, not one: each continues past the previous page's last id, and
    # the empty one at 6 is how the walk learns it is done.
    assert calls == [(0, 3), (3, 3), (6, 3)]
    assert reached == ids


async def test_a_user_who_blocked_the_bot_is_counted_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most common outcome of a broadcast is not an error: those users are
    unreachable, and the report says so without calling the run broken."""
    _install(monkeypatch, [1, 2, 3])
    bot = RecordingBot({2: [_blocked()]})

    report = await broadcast.deliver(bot, object(), "hi", interval_s=0)  # type: ignore[arg-type]

    assert [chat_id for chat_id, _ in bot.sent] == [1, 3]
    assert (report.sent, report.blocked, report.failed) == (2, 1, 0)


async def test_a_real_failure_is_reported_and_the_run_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [1, 2, 3])
    bot = RecordingBot({2: [_bad_request()]})

    report = await broadcast.deliver(bot, object(), "hi", interval_s=0)  # type: ignore[arg-type]

    assert [chat_id for chat_id, _ in bot.sent] == [1, 3]
    assert (report.sent, report.blocked, report.failed) == (2, 0, 1)
    assert not report.ok


async def test_an_unexpected_error_is_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [1, 2])
    bot = RecordingBot({1: [RuntimeError("boom")]})

    report = await broadcast.deliver(bot, object(), "hi", interval_s=0)  # type: ignore[arg-type]

    assert (report.sent, report.failed) == (1, 1)


async def test_telegrams_rate_limit_is_obeyed_not_fought(
    monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]
) -> None:
    """The API says how long to wait; waiting that long is the whole fix."""
    _install(monkeypatch, [7])
    bot = RecordingBot({7: [_rate_limited(3)]})

    report = await broadcast.deliver(bot, object(), "hi", interval_s=0)  # type: ignore[arg-type]

    assert bot.sent == [(7, "hi")]
    assert report.sent == 1 and report.failed == 0
    assert 3.0 < _no_real_sleeps[0]  # the API's number, not a guess of our own


async def test_a_rate_limit_that_keeps_happening_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting twice for the same recipient means something else is wrong — the
    recipient is given up on rather than blocking everybody behind them."""
    _install(monkeypatch, [7, 8])
    bot = RecordingBot({7: [_rate_limited(1), _rate_limited(1)]})

    report = await broadcast.deliver(bot, object(), "hi", interval_s=0)  # type: ignore[arg-type]

    assert (report.sent, report.failed) == (1, 1)
    assert [chat_id for chat_id, _ in bot.sent] == [8]


async def test_sends_are_paced(monkeypatch: pytest.MonkeyPatch, _no_real_sleeps: list[float]) -> None:
    """Telegram's ceiling is about 30/s per bot; the run stays well under it."""
    _install(monkeypatch, [1, 2, 3])
    bot = RecordingBot()

    await broadcast.deliver(bot, object(), "hi", interval_s=0.05)  # type: ignore[arg-type]

    assert _no_real_sleeps == [0.05, 0.05, 0.05]


async def test_the_admin_watching_gets_the_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress is throttled to every Nth send (editing on every one would burn the
    rate limit the pacing exists for) and then stated once more at the end."""
    _install(monkeypatch, [1, 2, 3])
    monkeypatch.setattr(broadcast, "PROGRESS_EVERY", 2)
    bot = RecordingBot({2: [_blocked()]})
    seen: list[tuple[int, int]] = []

    async def progress(sent: int, total: int) -> None:
        seen.append((sent, total))

    await broadcast.deliver(bot, object(), "hi", on_progress=progress, interval_s=0)  # type: ignore[arg-type]

    assert seen == [(1, 2), (2, 3)], "the periodic update, then the final one"


async def test_a_progress_callback_that_fails_does_not_stop_the_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [1, 2])
    bot = RecordingBot()

    async def progress(sent: int, total: int) -> None:
        raise RuntimeError("the editing message went away")

    report = await broadcast.deliver(bot, object(), "hi", on_progress=progress, interval_s=0)  # type: ignore[arg-type]

    assert report.sent == 2


async def test_an_empty_database_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [])
    bot = RecordingBot()

    report = await broadcast.deliver(bot, object(), "hi", interval_s=0)  # type: ignore[arg-type]

    assert report.total == 0 and bot.sent == []
