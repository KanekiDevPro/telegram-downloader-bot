"""A cached ``telegram_file_id`` is instant delivery — or the run goes fresh.

The smart cache's contract, pinned at the two seams where it decides everything.
On a *tap* (``_submit`` — where every format choice funnels) a cached row must
replay at once and never reach the queue; a miss queues the heavy job instead;
and a row whose id Telegram refuses is dropped before the fresh run. In the
*worker* the same check guards the extractor itself: a hit replays without
extracting or downloading anything, a miss runs the whole pipeline and then
remembers the id Telegram actually accepted (write-on-success), and a dead id
is forgotten before that fresh run replaces it.

Everything below runs the *real* cache and delivery code against a fake pool
and a fake Telegram — only the network and the database are simulated. What is
deliberately not here: how a row is sent (``tests/test_delivery.py``), how it is
keyed (``tests/test_cache_key.py``) and what its caption says
(``tests/test_cache_title.py``) have their own contract files.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, User

from core import database
from handlers import user as user_module
from services import worker
from services.extractor import DownloadResult, MediaInfo
from services.queue import DownloadTask

URL = "https://youtu.be/abc"
#: The id a past upload left behind — what a replay must travel on.
CACHED_ID = "AgAC-cached"


def _bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=cast("Any", None), message="Bad Request: wrong file identifier")


def _row(**fields: Any) -> dict[str, Any]:
    """A cache row, as ``services/delivery.py`` consumes one (a mapping works)."""
    return {
        "url_hash": "h",
        "original_url": URL,
        "platform": "youtube",
        "telegram_file_id": CACHED_ID,
        "quality": "video:360p",
        "kind": "video",
        "title": "A Clip",
        "label": "360p",
        **fields,
    }


class _Pool:
    """Answers the cache lookup; remembers every statement it was handed."""

    def __init__(self, cache_row: dict[str, Any] | None = None) -> None:
        self.cache_row = cache_row
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def _note(self, query: str, args: tuple[Any, ...]) -> None:
        self.queries.append((query, args))

    async def execute(self, query: str, *args: Any) -> str:
        self._note(query, args)
        return "UPDATE 1"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self._note(query, args)
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._note(query, args)
        return 1

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self._note(query, args)
        if "smart_cache" in query:
            return self.cache_row
        if "users" in query:
            return {
                "telegram_id": args[0] if args else 5,
                "is_premium": True,
                "premium_until": None,
                "language": "fa",
                "daily_downloads": 0,
                "last_download_date": None,
            }
        return None

    def writes(self, verb: str) -> list[tuple[str, tuple[Any, ...]]]:
        return [(query, args) for query, args in self.queries if verb in query]


# ---------------------------------------------------------------------------
# Seam 1 — the tap: replay now, or queue the heavy job
# ---------------------------------------------------------------------------


class TapBot:
    """Telegram at the tap: the replay's ``send_video``/``send_document`` calls,
    plus the method objects the aiogram message shortcuts hand to the bot."""

    def __init__(self, *, refusing: bool = False) -> None:
        self.sends: list[tuple[str, Any]] = []
        self.methods: list[Any] = []
        self.refusing = refusing

    def _send(self, kind: str, payload: Any) -> Any:
        if self.refusing:
            raise _bad_request()
        self.sends.append((kind, payload))
        return SimpleNamespace(**{kind: SimpleNamespace(file_id=f"{kind[0]}-fresh")})

    async def __call__(self, method: Any) -> Any:
        self.methods.append(method)
        return True

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Any:
        return self._send("video", video)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        return self._send("document", document)


class _TapQueue:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


class _NoRefusal:
    """``preflight`` with nothing to say — the real rules have their own tests."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(refused=False, message="")


def _user() -> dict[str, Any]:
    return {"telegram_id": 4242, "is_premium": False, "premium_until": None, "language": "fa"}


@pytest.fixture(autouse=True)
def _quiet_tap(monkeypatch: pytest.MonkeyPatch) -> None:
    """No preflight, and the double-tap guard forgets every earlier test's taps."""
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    user_module._recent_requests.clear()


async def _tap(bot: TapBot, pool: _Pool) -> _TapQueue:
    """One format tap through the real submit path (``tap=None``: no button)."""
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=4242, type="private"),
        from_user=User(id=4242, is_bot=False, first_name="u"),
    ).as_(cast(Bot, bot))
    queue = _TapQueue()
    await user_module._submit(
        cast(Bot, bot),
        message,
        cast(Any, pool),
        cast(Any, queue),
        cast(Any, _user()),
        URL,
        "video",
        "360p",
        "fa",
        tap=None,
    )
    return queue


async def test_a_cached_row_replays_on_the_tap_without_queueing_a_download() -> None:
    """Instant delivery: the row *is* the file — the heavy job never starts."""
    bot = TapBot()
    pool = _Pool(cache_row=_row())

    queue = await _tap(bot, pool)

    assert bot.sends == [("video", CACHED_ID)], "send_video with the stored id, nothing fetched"
    assert queue.tasks == [], "a hit never queues: there is nothing to extract or download"


async def test_a_tap_without_a_cached_row_queues_the_fresh_cycle() -> None:
    """Cache miss: the download/upload cycle is the fallback, and it is queued."""
    bot = TapBot()
    pool = _Pool(cache_row=None)

    queue = await _tap(bot, pool)

    assert bot.sends == [], "nothing was replayable"
    assert len(queue.tasks) == 1, "the miss falls back to a fresh extraction and download"


async def test_a_dead_file_id_is_cleared_and_the_fresh_cycle_runs() -> None:
    """Invalidation: Telegram refused the stored id — drop the row and run fresh."""
    bot = TapBot(refusing=True)
    pool = _Pool(cache_row=_row())

    queue = await _tap(bot, pool)

    assert pool.writes("DELETE FROM smart_cache"), "the stale row is cleared, not retried forever"
    assert len(queue.tasks) == 1, "and the request falls back to a fresh download/upload cycle"


# ---------------------------------------------------------------------------
# Seam 2 — the worker: replay before extraction, remember after upload
# ---------------------------------------------------------------------------


class WorkerBot:
    """Telegram at the worker seam: fresh uploads succeed; a stale id is refused."""

    def __init__(self, *, dead_ids: tuple[str, ...] = ()) -> None:
        self.uploads: list[tuple[str, Any]] = []
        self.dead_ids = set(dead_ids)
        self.messages: list[str] = []

    def _send(self, kind: str, payload: Any) -> Any:
        if payload in self.dead_ids:
            raise _bad_request()
        self.uploads.append((kind, payload))
        return SimpleNamespace(**{kind: SimpleNamespace(file_id="fresh-1")})

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.messages.append(text)
        return SimpleNamespace(edit_text=self._edit)

    async def _edit(self, text: str = "", **kwargs: Any) -> None:
        return None

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Any:
        return self._send("video", video)

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> Any:
        return self._send("audio", audio)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        return self._send("document", document)

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Any:
        return self._send("photo", photo)

    async def send_media_group(self, chat_id: int, media: Any, **kwargs: Any) -> Any:
        self.uploads.append(("photo_group", media))
        return [SimpleNamespace(photo=[SimpleNamespace(file_id="fresh-1")])]


class _WorkerExtractor:
    cookie_file: Any = None

    def __init__(self, download_dir: Path, extract_log: list[str]) -> None:
        self.download_dir = download_dir
        self.extract_log = extract_log

    async def extract(self, url: str) -> MediaInfo:
        self.extract_log.append(url)
        return _info()

    async def download(
        self, url: str, media_format: str, quality: str, **kwargs: Any
    ) -> DownloadResult:
        job = self.download_dir / "job"
        job.mkdir(parents=True, exist_ok=True)
        media = job / "clip.mp4"
        media.write_bytes(b"x" * 2048)
        return DownloadResult(
            file_path=media,
            info=_info(),
            media_format=media_format,  # type: ignore[arg-type]
            quality=quality,
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
        filesize_approx=2048,
        is_live=False,
    )


class _Settings:
    upload_limit_bytes = 10**9
    max_file_size_mb = 2000

    def __getattr__(self, name: str) -> Any:
        return None  # anything the flow does not need reads as unset


class _WorkerQueue:
    async def requeue(self, task: DownloadTask) -> None:
        return None

    async def release(self, task: DownloadTask) -> None:
        return None


class _NeverStopping:
    def is_set(self) -> bool:
        return False

    async def wait(self) -> None:
        await _never()


async def _never() -> None:
    import asyncio

    await asyncio.Event().wait()


def _task() -> DownloadTask:
    return DownloadTask(
        chat_id=5,
        telegram_id=5,
        url=URL,
        media_format="video",
        quality="360p",
        lang="fa",
        title="A Clip",
        chat_title="",
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    cache_row: dict[str, Any] | None,
    dead_ids: tuple[str, ...] = (),
) -> tuple[WorkerBot, _Pool, list[str]]:
    monkeypatch.setattr(worker, "get_settings", lambda: _Settings())
    bot = WorkerBot(dead_ids=dead_ids)
    pool = _Pool(cache_row=cache_row)
    log: list[str] = []
    await worker._process_with_retry(
        _task(),
        cast(Any, bot),
        cast(Any, pool),
        cast(Any, _WorkerQueue()),
        cast(Any, _WorkerExtractor(download_dir=tmp_path, extract_log=log)),
        cast(Any, _NeverStopping()),
    )
    return bot, pool, log


async def test_the_worker_replays_a_cached_row_without_extracting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The heavy part never starts: no extraction, no download — just the file."""
    bot, pool, log = await _run(monkeypatch, tmp_path, cache_row=_row())

    assert log == [], "the extractor is never touched — that is the whole point"
    assert bot.uploads == [("video", CACHED_ID)], "the stored id is re-sent as-is"
    assert not pool.writes("INSERT INTO smart_cache"), "a replay has nothing new to remember"


async def test_a_miss_downloads_and_remembers_the_id_telegram_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Write-on-success: the row carries the id of what really reached the chat."""
    bot, pool, log = await _run(monkeypatch, tmp_path, cache_row=None)

    assert log == [URL], "a miss runs the full extraction"
    assert bot.uploads and bot.uploads[0][0] == "video", "and uploads the fresh file"
    stored = pool.writes("INSERT INTO smart_cache")
    assert stored, "the successful upload is remembered"
    _, args = stored[0]
    assert args[3] == "fresh-1", "the file_id Telegram accepted, not one we invented"
    assert args[5] == "video", "and how to send it again"


async def test_the_worker_forgets_a_dead_id_before_the_fresh_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Invalidation at the worker seam: clear the row, extract, upload, re-store."""
    bot, pool, log = await _run(
        monkeypatch, tmp_path, cache_row=_row(), dead_ids=(CACHED_ID,)
    )

    assert pool.writes("DELETE FROM smart_cache"), "the dead id is dropped"
    assert log == [URL], "and the job falls back to a fresh extraction"
    assert bot.uploads and bot.uploads[0][0] == "video", "a fresh upload replaces the stale id"
    assert bot.uploads[0][1] != CACHED_ID, "the dead id is never sent again"
    stored = pool.writes("INSERT INTO smart_cache")
    assert stored and stored[0][1][3] == "fresh-1", "and the replacement id is remembered"
    verbs = [query for query, _ in pool.queries if "smart_cache" in query]
    assert verbs.index(next(q for q in verbs if "DELETE" in q)) < verbs.index(
        next(q for q in verbs if "INSERT" in q)
    ), "forget comes before the fresh run, not after it"


# ---------------------------------------------------------------------------
# The schema the whole thing stands on
# ---------------------------------------------------------------------------


def test_the_schema_has_a_place_for_the_file_id() -> None:
    schema = database.SCHEMA_SQL
    assert "CREATE TABLE IF NOT EXISTS smart_cache" in schema
    assert "telegram_file_id" in schema
    assert "ADD COLUMN IF NOT EXISTS" in schema, "later columns arrive as safe migrations"
