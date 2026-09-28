"""A shutdown during a download may cost a wait — never the job.

Two promises. A worker cancelled mid-download hands its task back to the queue,
quota claim included, instead of dropping it: without this the restart left the
user's card on ⏳ forever with the job lock held for its TTL. And an abandoned
download stops writing: the timeout (or the cancellation) arms the progress
hook, and a hook that raises is the one "stop" yt-dlp answers to — with the job
directory removed behind the stopped thread.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot

from services import worker
from services.extractor import ExtractionError, ExtractorService
from services.queue import DownloadTask

URL = "https://youtu.be/abc"
USER_ID = 4242
FA = "fa"
PRE_JOB = 1
LIMIT = 10

CLAIM = "daily_downloads = CASE"
REFUND = "daily_downloads = daily_downloads - 1"

#: The one quota day these tests live in — see tests/test_quota_refund.py: the
#: seed and the claim read the same pinned day by construction, never the two
#: calendars (system clock vs configured zone) that disagree near midnight.
FROZEN_DAY = date(2026, 9, 29)


@pytest.fixture(autouse=True)
def _frozen_quota_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "today_local", lambda: FROZEN_DAY)
    monkeypatch.setattr("core.database.today_local", lambda: FROZEN_DAY)



class _Pool:
    """A users table in memory — the claim and refund statements really apply."""

    def __init__(self) -> None:
        self.users: dict[int, dict[str, Any]] = {
            USER_ID: {
                "telegram_id": USER_ID,
                "is_premium": False,
                "premium_until": None,
                "language": FA,
                "daily_downloads": PRE_JOB,
                "last_download_date": FROZEN_DAY,
            }
        }
        self.statements: list[str] = []

    @property
    def daily(self) -> int:
        return self.users[USER_ID]["daily_downloads"]

    def count(self, fragment: str) -> int:
        return sum(fragment in statement for statement in self.statements)

    def _note(self, query: str) -> str:
        flat = " ".join(query.split())
        self.statements.append(flat)
        return flat

    async def fetchrow(self, query: str, *args: Any) -> Any:
        flat = self._note(query)
        if flat.startswith("SELECT * FROM users"):
            return self.users.get(args[0])
        if CLAIM in flat:
            user = self.users[args[0]]
            if user["last_download_date"] != args[1]:
                user["daily_downloads"] = 1
            elif user["daily_downloads"] < args[2]:
                user["daily_downloads"] += 1
            else:
                return None
            user["last_download_date"] = args[1]
            return {"daily_downloads": user["daily_downloads"]}
        if REFUND in flat:
            user = self.users[args[0]]
            if user["last_download_date"] == args[1] and user["daily_downloads"] > 0:
                user["daily_downloads"] -= 1
                return {"daily_downloads": user["daily_downloads"]}
            return None
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self._note(query)
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._note(query)
        return 1

    async def execute(self, query: str, *args: Any) -> str:
        self._note(query)
        return "UPDATE 0"


class _Queue:
    def __init__(self) -> None:
        self.requeued: list[str] = []
        self.released: list[str] = []

    async def requeue(self, task: DownloadTask) -> None:
        self.requeued.append(task.url)

    async def release(self, task: DownloadTask) -> None:
        self.released.append(task.url)


class _Bot:
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        return SimpleNamespace(edit_text=self._edit, delete=self._delete)

    async def _edit(self, text: str = "", **kwargs: Any) -> None:
        return None

    async def _delete(self) -> None:
        return None

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> None:
        return None


class _HangingExtractor:
    """A download that never finishes — until its task is cancelled."""

    cookie_file: Any = None

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def download(
        self, url: str, media_format: str, quality: str, **kwargs: Any
    ) -> Any:
        self.started.set()
        await asyncio.Event().wait()


class _Settings:
    admin_ids: list[int] = []
    upload_limit_bytes = 10**9
    max_file_size_mb = 2000

    def __getattr__(self, name: str) -> Any:
        return None


class _NeverStopping:
    def is_set(self) -> bool:
        return False

    async def wait(self) -> None:
        await asyncio.Event().wait()


def _task() -> DownloadTask:
    return DownloadTask(
        url=URL,
        telegram_id=USER_ID,
        chat_id=USER_ID,
        media_format="video",
        quality="360p",
        lang=FA,
        title="A Clip",
        is_live=False,
        size_estimate=0,
    )


async def test_a_cancelled_job_is_handed_back_to_the_queue_with_its_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1.4, pinned: shutdown lands mid-download and the job is *not* lost —
    it is requeued whole (never settled), and the slot its abandoned attempt
    claimed goes back with it. Before this, the card sat on ⏳ forever while the
    job lock blocked every retry for its TTL."""
    monkeypatch.setattr(worker, "get_settings", lambda: _Settings())
    monkeypatch.setattr(worker, "effective_daily_limit", lambda user: LIMIT)
    pool = _Pool()
    queue = _Queue()
    extractor = _HangingExtractor()

    running = asyncio.create_task(
        worker._process_with_retry(
            _task(),
            cast(Bot, _Bot()),
            cast(Any, pool),
            cast(Any, queue),
            cast(Any, extractor),
            cast(Any, _NeverStopping()),
        )
    )
    await asyncio.wait_for(extractor.started.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert queue.requeued == [URL], "the interrupted job goes back on the queue"
    assert queue.released == [], "and keeps its claim — it is still one job"
    assert pool.count(CLAIM) == 1 and pool.count(REFUND) == 1
    assert pool.daily == PRE_JOB, "an attempt nobody waited for buys nothing"


class _HangingYDL:
    """A yt-dlp stand-in that "downloads" by ticking the progress hook until
    the hook itself says stop — exactly what a real yt-dlp run does."""

    def __init__(self, opts: dict[str, Any], made: "list[_HangingYDL]") -> None:
        self.opts = opts
        self.finished = False
        made.append(self)

    def __enter__(self) -> "_HangingYDL":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> Any:
        try:
            hooks = self.opts.get("progress_hooks") or []
            for _ in range(400):  # ~2s of ticks: long past any budget below
                for hook in hooks:
                    hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 2})
                time.sleep(0.005)
            raise AssertionError("the download never stopped writing")
        finally:
            self.finished = True


def _hanging_service(
    monkeypatch: pytest.MonkeyPatch,
    download_dir: Path,
    made: "list[_HangingYDL]",
    *,
    budget: float = 0.05,
) -> ExtractorService:
    service = ExtractorService(download_dir, cookie_file=None, js_runtime="none")
    service.download_timeout_s = budget  # type: ignore[assignment]
    monkeypatch.setattr(service, "_ydl", lambda opts: _HangingYDL(opts, made))
    return service


async def _until_stopped(made: "list[_HangingYDL]") -> None:
    for _ in range(200):  # up to ~2s: the abort lands on the hook's next tick
        if made and made[0].finished:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the abandoned download never stopped")


async def test_an_abandoned_download_stops_writing_and_clears_its_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The budget runs out mid-download: the run is failed (TIMEOUT to the
    caller), the still-running thread is told to stop — a hook that raises is
    the one "stop" yt-dlp answers to — and its job directory is removed."""
    made: list[_HangingYDL] = []
    service = _hanging_service(monkeypatch, tmp_path / "dl", made)

    with pytest.raises(ExtractionError) as caught:
        await service.download(URL, "video", "360p")

    assert caught.value.code == "TIMEOUT"
    await _until_stopped(made)
    assert list((tmp_path / "dl").glob("job-*")) == [], "no half-written directory left"


async def test_a_cancelled_download_stops_writing_and_clears_its_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shutdown case (the one the worker test above rides in on): cancelling
    the wait must stop the thread too, not leave it downloading into a directory
    the restart's retry will collide with."""
    made: list[_HangingYDL] = []
    # A budget far longer than the test: *cancellation* is what must stop it.
    service = _hanging_service(monkeypatch, tmp_path / "dl", made, budget=30.0)

    running = asyncio.create_task(service.download(URL, "video", "360p"))
    await asyncio.sleep(0.1)  # mid-download
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    await _until_stopped(made)
    assert list((tmp_path / "dl").glob("job-*")) == [], "the abandoned run cleans up"
