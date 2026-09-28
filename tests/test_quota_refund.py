"""A claimed daily-quota slot is given back when — and only when — it bought nothing.

Three promises. A job that dies delivering refunds its claim — every attempt of
it, so one bad link can never burn three slots and hand the user nothing. A
failure before the quota gate steals nothing (there is no claim to give back).
And a slot that already bought a delivery stays spent even when the bookkeeping
after the send fails: a cache write blipping after the bytes landed is not a
delivery failure. And a delivered job is *finished*: a bookkeeping blip after
the send never re-runs the download or the upload. The refund also keeps to its
own day — a claim that crossed midnight is never taken out of the new day's
counter.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from core import database
from services import worker
from services.extractor import DownloadResult, ExtractionError, MediaInfo
from services.queue import DownloadTask

URL = "https://youtu.be/abc"
USER_ID = 4242
FA = "fa"
#: The user had already downloaded once today before this job ran.
PRE_JOB = 1
#: High enough that the quota never decides these tests — only the refund does.
LIMIT = 10

#: The two statements the accounting turns on, as they appear in the SQL.
CLAIM = "daily_downloads = CASE"
REFUND = "daily_downloads = daily_downloads - 1"

#: The one quota day these tests live in. The code claims with ``today_local()``
#: — the *configured zone's* calendar — while the doubles used to be seeded
#: with ``date.today()``, the system clock's calendar. Two calendars that
#: disagree in a window around every midnight (and disagreed all night while
#: the tz database was missing) made these pins flaky by construction. Both
#: seams are frozen to this one day, so the seed and the claim can never
#: disagree again.
FROZEN_DAY = date(2026, 9, 29)


@pytest.fixture(autouse=True)
def _frozen_quota_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the quota day at the seam: the worker's claim clock and the
    database's own refund clock read the same pinned day, by construction."""
    monkeypatch.setattr(worker, "today_local", lambda: FROZEN_DAY)
    monkeypatch.setattr(database, "today_local", lambda: FROZEN_DAY)



def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(
        method=cast("Any", None), message="Bad Request: wrong file identifier"
    )


def _info() -> MediaInfo:
    return MediaInfo(
        source_url=URL,
        title="A Clip",
        platform="youtube",
        webpage_url=URL,
        extension="mp4",
        thumbnail=None,
        duration=95,
        # Deliberately smaller than any ceiling below: only the *final* size
        # check may refuse these tests' files, never the probe's early guess.
        filesize_approx=64,
        is_live=False,
    )


class _QuotaPool:
    """A users table in memory — the claim and refund statements really apply."""

    def __init__(
        self,
        *,
        today: date,
        daily: int = PRE_JOB,
        claimed_on: date | None = None,
    ) -> None:
        self.users: dict[int, dict[str, Any]] = {
            USER_ID: {
                "telegram_id": USER_ID,
                "is_premium": False,
                "premium_until": None,
                "language": FA,
                "daily_downloads": daily,
                "last_download_date": claimed_on if claimed_on is not None else today,
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
        if CLAIM in flat:  # can_claim_download
            user = self.users[args[0]]
            if user["last_download_date"] != args[1]:
                user["daily_downloads"] = 1
            elif user["daily_downloads"] < args[2]:
                user["daily_downloads"] += 1
            else:
                return None
            user["last_download_date"] = args[1]
            return {"daily_downloads": user["daily_downloads"]}
        if REFUND in flat:  # refund_download_claim
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


class _Bot:
    """Telegram at the worker seam: uploads land unless the bot refuses them."""

    def __init__(self, *, refusing: bool = False, no_file_id: bool = False) -> None:
        self.refusing = refusing
        self.no_file_id = no_file_id
        self.uploads: list[str] = []

    def _send(self, kind: str) -> Any:
        if self.refusing:
            raise _bad_request()
        self.uploads.append(kind)
        # ``no_file_id``: Telegram accepted the bytes but answered with no id.
        file_id = "" if self.no_file_id else f"{kind}-1"
        return SimpleNamespace(**{kind: SimpleNamespace(file_id=file_id)})

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        return SimpleNamespace(edit_text=self._edit, delete=self._delete)

    async def _edit(self, text: str = "", **kwargs: Any) -> None:
        return None

    async def _delete(self) -> None:
        return None

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Any:
        return self._send("video")

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> Any:
        return self._send("audio")

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        return self._send("document")


class _Extractor:
    cookie_file: Any = None

    def __init__(self, download_dir: Path, *, size: int = 2048) -> None:
        self.download_dir = download_dir
        self.size = size
        self.downloads = 0

    async def extract(self, url: str) -> MediaInfo:
        return _info()

    async def download(
        self, url: str, media_format: str, quality: str, **kwargs: Any
    ) -> DownloadResult:
        self.downloads += 1
        job = self.download_dir / "job"
        job.mkdir(parents=True, exist_ok=True)
        media = job / "clip.mp4"
        media.write_bytes(b"x" * self.size)
        return DownloadResult(
            file_path=media,
            info=_info(),
            media_format=media_format,  # type: ignore[arg-type]
            quality=quality,
        )


class _ProbeRefusing(_Extractor):
    """A link the probe rejects — before the quota gate is ever reached."""

    async def extract(self, url: str) -> MediaInfo:
        raise ExtractionError("UNSUPPORTED_URL", "not a video site")


class _Settings:
    admin_ids: list[int] = []
    max_file_size_mb = 2000

    def __init__(self, upload_limit_bytes: int = 10**9) -> None:
        self.upload_limit_bytes = upload_limit_bytes

    def __getattr__(self, name: str) -> Any:
        return None  # anything the flow does not need reads as unset


class _Queue:
    """The queue at the settle seam: every job that settled, in order."""

    def __init__(self) -> None:
        self.released: list[DownloadTask] = []

    async def requeue(self, task: DownloadTask) -> None:
        return None

    async def release(self, task: DownloadTask) -> None:
        self.released.append(task)


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
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    pool: _QuotaPool,
    bot: _Bot,
    extractor: _Extractor,
    *,
    upload_limit: int = 10**9,
    queue: _Queue | None = None,
) -> DownloadTask:
    """One job through the real retry wrapper — with no real sleeping between attempts."""

    async def _no_sleep(stop_event: Any, seconds: float) -> bool:
        return False

    monkeypatch.setattr(worker, "get_settings", lambda: _Settings(upload_limit))
    monkeypatch.setattr(worker, "effective_daily_limit", lambda user: LIMIT)
    monkeypatch.setattr(worker, "_sleep_until", _no_sleep)
    task = _task()
    await worker._process_with_retry(
        task,
        cast(Bot, bot),
        cast(Any, pool),
        cast(Any, queue if queue is not None else _Queue()),
        cast(Any, extractor),
        cast(Any, _NeverStopping()),
    )
    return task


async def test_a_delivery_failure_gives_every_claimed_slot_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1.2, pinned: a job Telegram refuses to deliver leaves the day's counter
    exactly where it started. Each attempt claimed its own slot (that is what a
    retry costs the user), so each gets its own back — one bad link can no longer
    burn three slots and hand over nothing."""
    pool = _QuotaPool(today=FROZEN_DAY)
    extractor = _Extractor(tmp_path)

    await _run(monkeypatch, pool, _Bot(refusing=True), extractor)

    assert extractor.downloads == 3, "the retry wrapper really ran all its attempts"
    assert pool.count(CLAIM) == 3, "three attempts, three claims"
    assert pool.count(REFUND) == 3, "and every one of them comes back"
    assert pool.daily == PRE_JOB, "the counter is back to its pre-job value"


async def test_a_failure_before_the_quota_gate_steals_no_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The refund is not a license to decrement: a probe failure never claimed a
    slot, and giving one "back" would hand the user somebody else's."""
    pool = _QuotaPool(today=FROZEN_DAY)

    await _run(monkeypatch, pool, _Bot(), _ProbeRefusing(tmp_path))

    assert pool.count(CLAIM) == 0, "the quota gate was never reached"
    assert pool.count(REFUND) == 0, "so there is nothing to give back"
    assert pool.daily == PRE_JOB


async def test_a_cache_blip_after_a_delivery_keeps_the_slot_spent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bytes are with the user; only the remembering failed. That is not a
    delivery failure, so the slot stays paid — refunding it would make every
    delivered file free."""

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database blip")

    monkeypatch.setattr(worker.cache_service, "memorize", boom)
    pool = _QuotaPool(today=FROZEN_DAY)
    bot = _Bot()

    await _run(monkeypatch, pool, bot, _Extractor(tmp_path))

    assert bot.uploads, "the file really was delivered"
    assert pool.count(REFUND) == 0, "a delivered slot is never refunded"
    assert pool.daily > PRE_JOB, "and the day's counter keeps what it earned"


async def test_a_cache_blip_after_delivery_never_re_runs_the_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P1.3, pinned: caching is best-effort bookkeeping *after* the bytes are in
    the chat. A memorize failure used to bubble to the retry loop as an internal
    crash, and the whole job — download and upload — ran again: the user got the
    same file up to three times and paid up to three slots for it. A delivered
    job is finished: one download, one send, the spent slot kept, one settle."""

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("database blip")

    monkeypatch.setattr(worker.cache_service, "memorize", boom)
    pool = _QuotaPool(today=FROZEN_DAY)
    bot = _Bot()
    extractor = _Extractor(tmp_path)
    queue = _Queue()

    task = await _run(monkeypatch, pool, bot, extractor, queue=queue)

    assert extractor.downloads == 1, "the extraction ran exactly once — no retry"
    assert len(bot.uploads) == 1, "and the file was sent exactly once"
    assert pool.count(CLAIM) == 1, "one delivery consumes one slot, never three"
    assert pool.count(REFUND) == 0, "a delivered slot is never refunded"
    assert task.quota_held is False, "the slot is spent, not held"
    assert queue.released == [task], "the job settles — it does not bubble"


async def test_a_send_without_a_file_id_still_counts_as_delivered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other post-send blip, pinned the same way: Telegram accepted the bytes
    but returned no file_id. The file is in the chat — the job is DELIVERED — so
    nothing may re-run it (the chat would get the same file again) and its slot
    stays spent (a free retry would deliver twice for one payment)."""
    pool = _QuotaPool(today=FROZEN_DAY)
    bot = _Bot(no_file_id=True)
    extractor = _Extractor(tmp_path)
    queue = _Queue()

    task = await _run(monkeypatch, pool, bot, extractor, queue=queue)

    assert extractor.downloads == 1
    assert len(bot.uploads) == 1, "the send happened exactly once"
    assert pool.count(CLAIM) == 1
    assert pool.count(REFUND) == 0, "the delivered slot stays spent"
    assert task.quota_held is False
    assert queue.released == [task], "delivered is finished — no retry, no re-send"


async def test_the_final_size_ceiling_gives_the_slot_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A file the transport ceiling refuses settles quietly with no delivery —
    the slot must not survive the refusal."""
    pool = _QuotaPool(today=FROZEN_DAY)
    bot = _Bot()

    await _run(
        monkeypatch, pool, bot, _Extractor(tmp_path), upload_limit=100
    )

    assert bot.uploads == [], "nothing was delivered"
    assert pool.count(CLAIM) == 1
    assert pool.count(REFUND) == 1, "the ceiling refusal gives the slot back"
    assert pool.daily == PRE_JOB


async def test_a_refund_never_crosses_into_the_new_day() -> None:
    """The daily-rollover guard, pinned at the statement: a claim made yesterday
    is never taken out of today's counter — and the guard itself lives in the
    SQL, not only in the caller's good behaviour."""
    pool = _QuotaPool(
        today=FROZEN_DAY, daily=2, claimed_on=FROZEN_DAY - timedelta(days=1)
    )

    gave_back = await database.refund_download_claim(cast(Any, pool), USER_ID)

    assert gave_back is False
    assert pool.daily == 2, "yesterday's claim never moves today's counter"
    refund_sql = next(s for s in pool.statements if REFUND in s)
    assert "last_download_date" in refund_sql, "the refund is bound to its day"
    assert "daily_downloads > 0" in refund_sql, "and can never underflow"



async def test_a_job_that_crosses_midnight_keeps_its_claim_on_the_day_it_made_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The day guard end to end, through the real worker: a job that claims its
    slot before midnight and fails after it must never take the refund out of
    the new day's counter.

    The claim runs on day one (the worker's clock); every refund runs on day
    two (the database's own clock — this job spanned midnight). The guard in
    the SQL — not the caller's good behaviour — refuses to decrement a counter
    that no longer reads as that day: day one keeps its claims, day two's
    counter is never touched. This is what
    ``test_a_refund_never_crosses_into_the_new_day`` means when it is a whole
    job, not one statement, that crosses midnight."""
    day_one = date(2026, 9, 29)
    day_two = date(2026, 9, 30)
    monkeypatch.setattr(worker, "today_local", lambda: day_one)
    monkeypatch.setattr(database, "today_local", lambda: day_two)
    pool = _QuotaPool(today=day_one)

    await _run(monkeypatch, pool, _Bot(refusing=True), _Extractor(tmp_path))

    assert pool.count(CLAIM) == 3, "three attempts, three claims — all on day one"
    assert pool.count(REFUND) == 3, "every attempt asked for its slot back"
    assert pool.daily == PRE_JOB + 3, (
        "the day-one counter keeps its claims: a refund never crosses midnight"
    )
    assert pool.users[USER_ID]["last_download_date"] == day_one
