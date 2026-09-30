"""A duplicate single-flight trigger must never leave its card stuck.

_submit edits the card to the wait state and *then* pushes to the queue; when
the queue refuses the push (-1 - the same logical job is already in flight)
the card must still reach a terminal, truthful state. Truthful means: no outcome
is promised (the first job may yet succeed or fail), only where to look. The
refusal itself must change nothing else: no second download, no mode-counter
increment (job_started runs only below the push), no quota claim (worker-side
only).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.methods import EditMessageText
from aiogram.types import Chat, Message, User

from core.i18n import t
from handlers import user as user_module
from services import download_mode
from services.queue import MemoryTaskQueue

URL = "https://youtu.be/abc"
USER_ID = 4242


class _Pool:
    """Cache miss, ordinary user, remembers every statement."""

    def __init__(self) -> None:
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
        if "users" in query:
            return {
                "telegram_id": USER_ID,
                "is_premium": False,
                "premium_until": None,
                "language": "en",
                "daily_downloads": 0,
                "last_download_date": None,
            }
        return None

    def updates(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [(query, args) for query, args in self.queries if "UPDATE" in query]


class TapBot:
    """Collects the aiogram method calls the message shortcuts route through it."""

    def __init__(self) -> None:
        self.methods: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.methods.append(method)
        return True

    def edits(self) -> list[str]:
        return [m.text or "" for m in self.methods if isinstance(m, EditMessageText)]


class _NoRefusal:
    """preflight with nothing to say - the real rules have their own tests."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(refused=False, message="")


def _user() -> dict[str, Any]:
    return {
        "telegram_id": USER_ID,
        "is_premium": False,
        "premium_until": None,
        "language": "en",
    }


def _message(bot: TapBot) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="u"),
    ).as_(cast(Bot, bot))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    user_module._recent_requests.clear()
    download_mode.reset_for_tests()
    yield
    user_module._recent_requests.clear()
    download_mode.reset_for_tests()


async def _submit_twice(
    bot: TapBot,
    pool: _Pool,
    queue: MemoryTaskQueue,
    url: str = URL,
    lang: str = "en",
) -> None:
    """Two triggers for one logical job, past the double-tap window."""
    for _ in range(2):
        # The double-tap guard only swallows re-taps inside its 5s window; a
        # re-sent link past it reaches the queue, which refuses it as -1.
        user_module._recent_requests.clear()
        await user_module._submit(
            cast(Bot, bot),
            _message(bot),
            cast(Any, pool),
            cast(Any, queue),
            cast(Any, _user()),
            url,
            "video",
            "best",
            lang,
            tap=None,
        )


@pytest.mark.parametrize("lang", ["en", "fa"])
async def test_duplicate_trigger_lands_on_a_terminal_truthful_card(lang: str) -> None:
    bot = TapBot()
    await _submit_twice(bot, _Pool(), MemoryTaskQueue(), lang=lang)

    expected = t("work.already_running", lang)
    assert any(expected in text for text in bot.edits()), (
        "the refused trigger's card must name the already-running job"
    )


async def test_no_second_download_is_created_for_a_duplicate() -> None:
    bot = TapBot()
    queue = MemoryTaskQueue()
    await _submit_twice(bot, _Pool(), queue)

    assert queue._items.qsize() == 1, "one logical job means one queued task"


async def test_canonical_twins_share_one_job() -> None:
    """Tracking params never change the media - the twins collide, then refuse."""
    bot = TapBot()
    pool = _Pool()
    queue = MemoryTaskQueue()
    first = "https://www.youtube.com/watch?v=abc&utm_source=x"
    twin = "https://www.youtube.com/watch?v=abc&gclid=y"
    user_module._recent_requests.clear()
    await user_module._submit(
        cast(Bot, bot),
        _message(bot),
        cast(Any, pool),
        cast(Any, queue),
        cast(Any, _user()),
        first,
        "video",
        "best",
        "en",
        tap=None,
    )
    user_module._recent_requests.clear()
    await user_module._submit(
        cast(Bot, bot),
        _message(bot),
        cast(Any, pool),
        cast(Any, queue),
        cast(Any, _user()),
        twin,
        "video",
        "best",
        "en",
        tap=None,
    )

    assert queue._items.qsize() == 1
    assert any("already running" in text for text in bot.edits())


async def test_duplicate_leaves_the_mode_counter_balanced() -> None:
    """job_started runs only below a successful push - the refusal adds none."""
    bot = TapBot()
    await _submit_twice(bot, _Pool(), MemoryTaskQueue())

    assert download_mode.active_count(USER_ID, download_mode.YOUTUBE) == 1, (
        "exactly one in-flight job: the first push; the duplicate added nothing"
    )


async def test_duplicate_consumes_no_quota() -> None:
    """The claim lives worker-side (_claim_quota); the refused tap claims none."""
    bot = TapBot()
    pool = _Pool()
    await _submit_twice(bot, pool, MemoryTaskQueue())

    assert pool.updates() == [], "a refused trigger writes no quota claim"


async def test_already_running_promises_no_outcome() -> None:
    """True whether the first job later succeeds or fails: only where to look."""
    for lang in ("en", "fa"):
        text = t("work.already_running", lang)
        assert "❌" not in text and "✅" not in text
