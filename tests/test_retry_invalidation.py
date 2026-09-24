"""A retry is a fresh extraction — never a replay of the failure it retries.

The complaint this pins: after an extraction failure (an infrastructure outage
like the session server answering 503), pressing «تلاش دوباره»/«🔄 Try again»
came back with the *same* failure state instantly, as if it had been cached.
Three promises fix that. The download retry explicitly drops the request's
cache row and the remembered "anonymous requests are refused here" verdict
before anything else runs (``handlers.user.on_retry``) — so the retry can only
answer with a new extraction. A spammed retry button queues the job once: a key
is spent once, and a second key's press for the same request is swallowed by
the same double-tap guard a first tap gets. And the probe's retry button — the
one screen whose retry is not single-use — runs one lookup at a time: while a
fresh extraction is in the air, a spammed press is answered and dropped instead
of stacking a second concurrent extraction of the same link.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, User

import core.ui as retry_store
from core.config import Settings
from handlers import user as user_module
from handlers.user import DownloadStates
from services import cache as cache_service
from services import preflight as preflight_module
from services.queue import DownloadTask

USER_ID = 4242
FA = "fa"
URL = "https://youtu.be/abc"


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, answers truthfully."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.state: Any = SimpleNamespace()

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]


def _message(text: str, bot: RecordingBot) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=_message("card", bot),
    ).as_(cast(Bot, bot))


def _user() -> Any:
    return {
        "telegram_id": USER_ID,
        "username": "ali",
        "is_premium": False,
        "premium_until": None,
        "language": FA,
        "is_new": False,
    }


class FakeQueue:
    """Only the two queue facts the gateway needs (it never downloads)."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return 0

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return 0


class _Cache:
    """The smart cache in memory — what a retry must clear before re-running."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.forgot: list[str] = []

    async def get_cached(
        self, pool: Any, url: str, media_format: str = "video", quality: object = ""
    ) -> Any:
        return self.rows.get(cache_service.cache_key(url, media_format, quality))

    async def forget(
        self, pool: Any, url: str, media_format: str = "video", quality: object = ""
    ) -> None:
        key = cache_service.cache_key(url, media_format, quality)
        self.forgot.append(key)
        self.rows.pop(key, None)


class _NoVerdict:
    refused = False
    message = ""


class _NoRefusal:
    """``preflight`` with nothing to say — and a note of what was cleared."""

    cleared: list[bool] = []

    @classmethod
    def youtube_preflight(cls, url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return _NoVerdict()

    @classmethod
    def clear_anonymous_refusal(cls) -> None:
        cls.cleared.append(True)


@pytest.fixture(autouse=True)
def _clean_guard() -> Any:
    """Each test starts without the duplicate-tap and probe-flight memory."""
    user_module._recent_requests.clear()
    user_module._probes_running.clear()
    retry_store._retries.clear()
    preflight_module.clear_anonymous_refusal()
    _NoRefusal.cleared = []
    yield
    user_module._recent_requests.clear()
    user_module._probes_running.clear()
    retry_store._retries.clear()
    preflight_module.clear_anonymous_refusal()


@pytest.fixture(autouse=True)
def _cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quota reads and settings without a database or an environment.

    No fallback engine and no cookie jar: the preflight verdicts these tests
    assert are decided by the evidence they record, not by this machine.
    """

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 0, "last_download_date": None}

    def clean_settings() -> Settings:
        return Settings(  # type: ignore[call-arg]
            _env_file=None, COBALT_API_URL="", COOKIE_FILE=None
        )

    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    monkeypatch.setattr(user_module, "get_settings", clean_settings)


def _task() -> DownloadTask:
    return DownloadTask(
        url=URL,
        telegram_id=USER_ID,
        chat_id=USER_ID,
        media_format="video",
        quality="720",
        lang=FA,
        title="A Clip",
        status_message_id=1,
    )


def _retry_key(task: DownloadTask) -> str:
    return retry_store.remember_retry({"task": task.to_payload()}, owner=USER_ID)


def _wire_cache(monkeypatch: pytest.MonkeyPatch, cache: _Cache) -> None:
    monkeypatch.setattr(user_module.cache_service, "get_cached", cache.get_cached)
    monkeypatch.setattr(user_module.cache_service, "forget", cache.forget)


async def test_a_retry_drops_the_request_cache_row_before_the_fresh_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry explicitly invalidates the extraction cache for its request:
    a stale row must never answer the retry (a replay would "succeed" without
    extracting anything), and the remembered refusal verdict goes with it."""
    cache = _Cache()
    _wire_cache(monkeypatch, cache)
    replayed: list[Any] = []

    async def send_cached_file(bot: Any, chat_id: int, row: Any, caption: str = "") -> bool:
        replayed.append(row)
        return True

    monkeypatch.setattr(user_module, "send_cached_file", send_cached_file)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())

    stale_key = cache_service.cache_key(URL, "video", "720")
    cache.rows[stale_key] = {"telegram_file_id": "stale"}
    task = _task()
    key = _retry_key(task)
    bot = RecordingBot()
    queue = FakeQueue()

    await user_module.on_retry(
        _callback(bot, f"{user_module.RETRY_PREFIX}{key}"), _user(), object(), queue, bot, lang=FA
    )

    assert cache.forgot == [stale_key], "the request's cache row is dropped explicitly"
    assert _NoRefusal.cleared, "and the remembered refusal verdict with it"
    assert replayed == [], "a stale row never replays the retry"
    assert [(item.quality, item.status_message_id) for item in queue.tasks] == [("720", 1)], (
        "the job runs fresh — cache, quota and preflight all get their say again"
    )


async def test_a_retry_forgets_the_remembered_refusal_so_the_fresh_attempt_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cached failure state, pinned: a failed run records "anonymous
    requests are refused here", and that verdict alone would refuse the retry
    before any extraction runs. An explicit retry clears it — the fresh attempt
    is exactly what the button asks for, and the worker re-records the evidence
    if the refusal is still real."""
    cache = _Cache()
    _wire_cache(monkeypatch, cache)

    # The failed run's evidence, left behind exactly as services/worker.py does.
    preflight_module.note_anonymous_refusal()
    assert preflight_module.youtube_preflight(
        URL, None, fallback_available=False
    ).refused, "without the fix, the retry is refused instantly — the cached failure"

    task = _task()
    key = _retry_key(task)
    bot = RecordingBot()
    queue = FakeQueue()

    await user_module.on_retry(
        _callback(bot, f"{user_module.RETRY_PREFIX}{key}"), _user(), object(), queue, bot, lang=FA
    )

    assert [item.quality for item in queue.tasks] == ["720"], (
        "the retry runs instead of replaying the remembered refusal"
    )
    assert preflight_module.refusal_age_s() is None, "the cached verdict is gone"


async def test_spamming_the_retry_button_queues_the_job_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One retry, one job. A key is spent once (a second press of the same
    button finds nothing), and a *second* failure's key pressed while the same
    request is being re-run is swallowed by the double-tap guard — never a
    second concurrent extraction job."""
    cache = _Cache()
    _wire_cache(monkeypatch, cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    task = _task()
    key_one = _retry_key(task)
    key_two = _retry_key(task)  # two failures, two keys — the spam case
    bot = RecordingBot()
    queue = FakeQueue()

    await user_module.on_retry(
        _callback(bot, f"{user_module.RETRY_PREFIX}{key_one}"), _user(), object(), queue, bot, lang=FA
    )
    await user_module.on_retry(
        _callback(bot, f"{user_module.RETRY_PREFIX}{key_two}"), _user(), object(), queue, bot, lang=FA
    )

    assert len(queue.tasks) == 1, "one retry, one job"
    assert bot.answers[-1].text is None, "the repeat is swallowed, not nagged"

    # The spent key: a third press finds nothing to run at all.
    bot2 = RecordingBot()
    await user_module.on_retry(
        _callback(bot2, f"{user_module.RETRY_PREFIX}{key_one}"),
        _user(),
        object(),
        FakeQueue(),
        bot2,
        lang=FA,
    )
    assert bot2.answers[0].show_alert is True
    assert len(queue.tasks) == 1


class _BlockingExtractor:
    """A probe that starts, says so, and waits — the spam window, made real."""

    timeout_s = 60.0

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, url: str) -> Any:
        self.urls.append(url)
        self.started.set()
        await self.release.wait()
        raise RuntimeError("the lookup failed")


async def test_spamming_the_probe_retry_runs_one_extraction_at_a_time() -> None:
    """The probe's retry button is not single-use — that is where a spammed
    press used to stack concurrent extractions of the same link. One lookup at
    a time: extra presses are answered and dropped while the fresh extraction
    is in the air, and a *later* press re-extracts for real."""
    extractor = _BlockingExtractor()
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=extractor)
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(url=URL)

    async def press() -> None:
        await user_module.on_probe_retry(
            _callback(bot, user_module.PROBE_CALLBACK),
            state,
            _user(),
            object(),
            FakeQueue(),
            bot,
            lang=FA,
        )

    first = asyncio.create_task(press())
    await extractor.started.wait()
    await press()  # spam while the first lookup is still running
    assert extractor.urls == [URL], "a spammed press never stacks a second extraction"

    extractor.release.set()
    await first
    assert len(bot.answers) == 2, "both presses were answered"

    await press()  # the lookup answered — a later press is a real fresh extraction
    assert extractor.urls == [URL, URL]
