"""The end-to-end message-count contract: one download, two messages.

MESSAGE #1 is the media card (the state message everything narrates in) and
MESSAGE #2 is the delivered media. Nothing else may reach the chat — no
"please wait", no "queued", no "uploading", ever. Chat actions, callback
acknowledgements, edits of the card and log lines are not messages and do not
count; this test drives the real worker orchestration and counts what Telegram
would show.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services import worker
from services.extractor import DownloadResult, MediaInfo
from services.queue import DownloadTask

URL = "https://www.instagram.com/reel/xyz/"

#: Words that mean the chat is being spammed with progress theatre.
FORBIDDEN = (
    "please wait",
    "downloading",
    "uploading",
    "queued",
    "processing",
    "completed",
    "added to queue",
    "در حال دانلود",
    "در حال ارسال",
    "در حال پردازش",
    "لطفاً صبر",
    "در صف",
    "درخواست شما ثبت",
)


class Status:
    """The one message the job narrates in."""

    message_id = 7

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)


class FakeBot:
    """Telegram, as far as the contract cares: every visible send lands here."""

    def __init__(self) -> None:
        self.messages: list[str] = []  # user-visible text messages
        self.edits: list[str] = []  # rewrites of an existing message
        self.uploads: list[str] = []  # delivered media
        self.actions: list[Any] = []  # chat actions — NOT messages

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Status:
        self.messages.append(text)
        return Status()

    async def edit_message_text(self, text: str = "", **kwargs: Any) -> None:
        self.edits.append(text)

    async def __call__(self, method: Any) -> Any:
        """aiogram shortcuts may hand the method to the bot itself."""
        name = type(method).__name__.lower()
        if "edit" in name:
            self.edits.append(str(getattr(method, "text", "")))
            return None
        if "send" in name:
            self.messages.append(str(getattr(method, "text", "")))
            return Status()
        return None

    async def send_video(self, chat_id: int, video: Any = None, **kwargs: Any) -> Any:
        self.uploads.append("video")
        return SimpleNamespace(video=SimpleNamespace(file_id="v-1"))

    async def send_audio(self, chat_id: int, audio: Any = None, **kwargs: Any) -> Any:
        self.uploads.append("audio")
        return SimpleNamespace(audio=SimpleNamespace(file_id="a-1"))

    async def send_document(self, chat_id: int, document: Any = None, **kwargs: Any) -> Any:
        self.uploads.append("file")
        return SimpleNamespace(document=SimpleNamespace(file_id="d-1"))

    async def send_photo(self, chat_id: int, photo: Any = None, **kwargs: Any) -> Any:
        self.uploads.append("photo")
        return SimpleNamespace(photo=[SimpleNamespace(file_id="p-1")])

    async def send_media_group(self, chat_id: int, media: Any, **kwargs: Any) -> Any:
        self.uploads.append("photo_group")
        return [SimpleNamespace(photo=[SimpleNamespace(file_id="g-1")])]

    async def send_chat_action(self, *args: Any, **kwargs: Any) -> None:
        self.actions.append(args or kwargs)

    def visible(self) -> list[str]:
        """Everything a human reads — sent messages and rewritten ones alike."""
        return [*self.messages, *self.edits]


class _Pool:
    """Answers the questions the flow asks, and remembers the SQL it asked with."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []

    def _note(self, query: str, args: tuple[Any, ...]) -> None:
        self.rows.append((query, args))

    async def execute(self, query: str, *args: Any) -> str:
        self._note(query, args)
        return "UPDATE 1"  # truthy for every "did it claim?" check

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self._note(query, args)
        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self._note(query, args)
        return 1

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self._note(query, args)
        if "smart_cache" in query:
            return None  # cache miss: the honest default for these tests
        if "users" in query:
            return {
                "telegram_id": args[0] if args else 1,
                "is_premium": True,
                "premium_until": None,
                "language": "fa",
                "daily_downloads": 0,
                "last_download_date": None,
            }
        return None


class FakeExtractor:
    cookie_file: Any = None

    def __init__(self, download_dir: Path) -> None:
        self.download_dir = download_dir

    async def extract(self, url: str) -> MediaInfo:
        return MediaInfo(
            source_url=URL,
            title="A Reel",
            platform="instagram",
            webpage_url=URL,
            extension="mp4",
            thumbnail=None,
            duration=95,
            filesize_approx=2048,
            is_live=False,
        )

    async def download(
        self, url: str, media_format: str, quality: str, *, progress_hook: Any = None, **kwargs: Any
    ) -> DownloadResult:
        job = self.download_dir / "job"
        job.mkdir(parents=True, exist_ok=True)
        media = job / "clip.mp4"
        media.write_bytes(b"x" * 2048)
        return DownloadResult(
            file_path=media,
            info=MediaInfo(
                source_url=URL,
                title="A Reel",
                platform="instagram",
                webpage_url=URL,
                extension="mp4",
                thumbnail=None,
                duration=95,
                filesize_approx=2048,
                is_live=False,
            ),
            media_format=media_format,  # type: ignore[arg-type]
            quality=quality,
        )


class _Settings:
    upload_limit_bytes = 10**9
    max_file_size_mb = 2000

    def __getattr__(self, name: str) -> Any:
        return None  # anything the flow does not need reads as unset


class _Queue:
    async def requeue(self, task: DownloadTask) -> None:
        return None


class _NeverStopping:
    def is_set(self) -> bool:
        return False

    async def wait(self) -> None:
        """Never fires — the timeout around it always wins."""
        await asyncio.Event().wait()


def _task(chat_id: int = 5, **over: Any) -> DownloadTask:
    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "telegram_id": 5,
        "url": URL,
        "media_format": "video",
        "quality": "360p",
        "lang": "fa",
        "title": "A Reel",
        "chat_title": "Group Chat" if chat_id < 0 else "",
    }
    fields.update(over)
    return DownloadTask(**fields)


async def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, task: DownloadTask) -> FakeBot:
    monkeypatch.setattr(worker, "get_settings", lambda: _Settings())
    bot = FakeBot()
    pool = _Pool()
    await worker._process_with_retry(
        task,
        bot,  # type: ignore[arg-type]
        pool,
        _Queue(),  # type: ignore[arg-type]
        FakeExtractor(download_dir=tmp_path),  # type: ignore[arg-type]
        _NeverStopping(),  # type: ignore[arg-type]
    )
    _assert_clean(bot)
    return bot


def _assert_clean(bot: FakeBot) -> None:
    """No progress sentence may reach the chat, at any point, in any wording."""
    for text in bot.visible():
        lowered = text.casefold()
        for word in FORBIDDEN:
            assert word not in lowered, f"«{word}» reached the chat: {text!r}"


async def test_a_private_download_is_exactly_two_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bot = await _run(monkeypatch, tmp_path, _task())
    assert len(bot.messages) == 1, f"MESSAGE #1 is the card: {bot.messages!r}"
    assert bot.uploads == ["video"], "MESSAGE #2 is the media file"
    assert len(bot.messages) + len(bot.uploads) == 2


async def test_the_gateway_card_is_edited_not_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With the card already on screen the worker opens none — it only narrates."""
    bot = await _run(monkeypatch, tmp_path, _task(status_message_id=42))
    assert bot.messages == [], "the card already exists; the worker sends nothing new"
    assert bot.uploads == ["video"], "the media is the one message this job adds"
    assert bot.edits, "the state (⏳) lives on the existing card"


async def test_a_group_download_adds_no_group_chatter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The group flow is the same orchestration: one card, one media, silence."""
    monkeypatch.setattr(worker, "get_settings", lambda: _Settings())
    bot = FakeBot()
    pool = _Pool()
    task = _task(chat_id=-1009876543210)
    await worker._process_with_retry(
        task,
        bot,  # type: ignore[arg-type]
        pool,
        _Queue(),  # type: ignore[arg-type]
        FakeExtractor(download_dir=tmp_path),  # type: ignore[arg-type]
        _NeverStopping(),  # type: ignore[arg-type]
    )
    _assert_clean(bot)
    assert len(bot.messages) == 1
    assert bot.uploads == ["video"]
    # …and the outcome went to the group analytics, quietly.
    recorded = [args for query, args in pool.rows if "group_downloads" in query]
    assert recorded, "a group download must be recorded for the panel"
    assert recorded[0][0] == -1009876543210
    assert recorded[0][2] is True
