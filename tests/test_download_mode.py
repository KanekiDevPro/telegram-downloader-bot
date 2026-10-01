"""Explicit download-mode/session isolation (TDD).

When a user taps a platform section the bot enters that section's mode and,
while the mode is active, only compatible URLs are accepted: Spotify mode
takes Spotify links, YouTube mode takes YouTube links, and a wrong-source link
is answered fast — never queued, never cached, never probed. The mode lives in
the user's own FSM data (never global), survives across multiple URLs, stays
locked while relevant jobs run, unlocks when they are gone, and a stale format
callback from an earlier mode is rejected safely.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.config import Settings
from core.utils import today_local
from handlers import user as user_module
from handlers.user import DownloadStates
from services import cache as cache_service
from services import download_mode
from services import preflight as preflight_module
from services import subscription as subscription_module

USER_A = 4242
USER_B = 4343
EN = "en"
FA = "fa"

SPOTIFY_1 = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
SPOTIFY_2 = "https://open.spotify.com/track/5uLU6hMCjMI75M1A2tKUQD"
SPOTIFY_3 = "https://open.spotify.com/track/6uLU6hMCjMI75M1A2tKUQE"
SPOTIFY_4 = "https://open.spotify.com/track/7uLU6hMCjMI75M1A2tKUQF"
YOUTUBE_1 = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTUBE_2 = "https://youtu.be/dQw4w9WgXcQ"
YOUTUBE_3 = "https://www.youtube.com/shorts/abc123XYZ01"
TIKTOK_1 = "https://www.tiktok.com/@username/video/7200000000000000000"


class RecordingBot:
    """Records bot API calls; answers messages with a real Message."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return _message(method.text or "", self)
        return True

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]


def _message(text: str, bot: RecordingBot, user_id: int = USER_A) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=cast(Any, "private")),
        from_user=User(id=user_id, is_bot=False, first_name="user"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, user_id: int = USER_A) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=_message("menu", bot, user_id),
    ).as_(cast(Bot, bot))


def _fresh_state(user_id: int = USER_A) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


def _user(user_id: int = USER_A) -> Any:
    return {
        "telegram_id": user_id,
        "username": "ali",
        "is_premium": False,
        "premium_until": None,
        "language": EN,
        "is_new": False,
    }


class FakeQueue:
    """The queue stand-in — records every enqueued task."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return len(self.tasks)

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


@pytest.fixture(autouse=True)
def _clean_mode_state() -> Any:
    """Each test starts with no modes and no active jobs anywhere."""
    download_mode.reset_for_tests()
    user_module._recent_requests.clear()
    yield
    download_mode.reset_for_tests()
    user_module._recent_requests.clear()


def _stub_intake_tail(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Replace the slow/network parts past the mode gate.

    The format-question branch is recorded (``accepted``); the auto-download
    branches (solo/Instagram/TikTok) run the *real* ``_submit`` so the tests
    prove the enqueue and the mode job-counting, with only its own slow/DB
    dependencies stubbed.
    """
    seen: dict[str, list[str]] = {"accepted": [], "probed": [], "cached": []}

    async def fake_ask(
        message: Message,
        state: FSMContext,
        user: Any,
        url: str,
        lang: str,
        **kwargs: Any,
    ) -> None:
        seen["accepted"].append(url)
        await state.set_state(DownloadStates.waiting_format)
        await state.update_data(url=url)

    async def fake_cached_rows(pool: Any, url: str) -> list[Any]:
        seen["cached"].append(url)
        return []

    async def fake_probe_supported(url: str) -> bool:
        seen["probed"].append(url)
        return True

    async def fake_canonical(url: str) -> str:
        return url

    async def fake_disabled(pool: Any) -> tuple[str, ...]:
        return ()

    async def fake_get_cached(pool: Any, *args: Any, **kwargs: Any) -> None:
        return None

    async def fake_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 0, "last_download_date": today_local()}

    def clean_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    def fake_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(refused=False, message="")

    monkeypatch.setattr(user_module, "_ask_about_link", fake_ask)
    monkeypatch.setattr(user_module, "_cached_rows", fake_cached_rows)
    monkeypatch.setattr(user_module, "_probe_supported", fake_probe_supported)
    monkeypatch.setattr(user_module, "_canonical_url", fake_canonical)
    monkeypatch.setattr(user_module, "_disabled_platforms", fake_disabled)
    monkeypatch.setattr(cache_service, "get_cached", fake_get_cached)
    monkeypatch.setattr(user_module.database, "get_daily_usage", fake_daily_usage)
    monkeypatch.setattr(user_module, "get_settings", clean_settings)
    monkeypatch.setattr(subscription_module, "get_settings", clean_settings)
    monkeypatch.setattr(preflight_module, "youtube_preflight", fake_preflight)
    return seen


async def _tap_platform(
    bot: RecordingBot, state: FSMContext, name: str, user_id: int = USER_A, lang: str = EN
) -> None:
    await user_module.on_menu_platform(
        _callback(bot, f"{user_module.PLATFORM_PREFIX}{name}", user_id),
        state,
        pool=None,
        lang=lang,
    )


async def _send_url(
    bot: RecordingBot,
    state: FSMContext,
    queue: FakeQueue,
    url: str,
    user_id: int = USER_A,
    lang: str = EN,
) -> None:
    await user_module._intake_flow(
        _message(url, bot, user_id),
        state,
        _user(user_id),
        url,
        lang,
        bot=cast(Bot, bot),
        pool=None,
        queue=cast(Any, queue),
    )


async def _mode_of(state: FSMContext) -> str:
    return download_mode.normalize((await state.get_data()).get(download_mode.MODE_KEY))


# ---------------------------------------------------------------------------
# Spotify mode
# ---------------------------------------------------------------------------


async def test_none_to_spotify_on_platform_tap() -> None:
    """Tapping the Spotify section activates Spotify mode for that user."""
    bot, state = RecordingBot(), _fresh_state()
    assert await _mode_of(state) == download_mode.NONE

    await _tap_platform(bot, state, "spotify")

    assert await _mode_of(state) == download_mode.SPOTIFY


async def test_spotify_mode_accepts_spotify_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Spotify link inside Spotify mode flows into the intake (once)."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "spotify")

    await _send_url(bot, state, queue, SPOTIFY_1)

    assert seen["accepted"] == [SPOTIFY_1]
    assert await _mode_of(state) == download_mode.SPOTIFY


async def test_spotify_mode_rejects_youtube_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A YouTube link inside Spotify mode is refused with a clear message."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "spotify", lang=FA)

    await _send_url(bot, state, queue, YOUTUBE_1, lang=FA)

    assert seen["accepted"] == []
    assert queue.tasks == []
    assert any("اسپاتیفای" in text for text in bot.texts)
    assert await _mode_of(state) == download_mode.SPOTIFY


async def test_intake_refuses_a_private_host_url_in_both_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A literal-private link is refused at intake: no queue, no probe, no leak."""
    from core.i18n import t

    for lang, needle in ((EN, "private or internal"), (FA, "خصوصی یا داخلی")):
        seen = _stub_intake_tail(monkeypatch)
        bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

        await _send_url(bot, state, queue, "http://127.0.0.1/video.mp4", lang=lang)

        assert seen["accepted"] == []
        assert seen["probed"] == []
        assert queue.tasks == []
        assert t("intake.private_host", lang) in bot.texts
        assert needle in bot.texts[0]
        assert "127.0.0.1" not in " ".join(bot.texts)


async def test_spotify_mode_accepts_multiple_spotify_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four Spotify links in a row — no reselecting the button in between."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "spotify")

    for url in (SPOTIFY_1, SPOTIFY_2, SPOTIFY_3, SPOTIFY_4):
        await _send_url(bot, state, queue, url)

    assert seen["accepted"] == [SPOTIFY_1, SPOTIFY_2, SPOTIFY_3, SPOTIFY_4]
    assert await _mode_of(state) == download_mode.SPOTIFY


async def test_youtube_format_tap_queues_and_counts_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The format tap inside YouTube mode enqueues and locks the mode."""
    _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "youtube")
    await _send_url(bot, state, queue, YOUTUBE_1)
    await state.update_data(offered=["720"], options=[(720, 720, 0, False)])

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720", USER_A),
        state,
        _user(USER_A),
        None,
        cast(Any, queue),
        cast(Bot, bot),
        lang=EN,
    )

    assert [task.url for task in queue.tasks] == [YOUTUBE_1]
    assert download_mode.active_count(USER_A, download_mode.YOUTUBE) == 1
    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_spotify_mode_stays_active_while_jobs_exist() -> None:
    """Switching to YouTube while Spotify jobs run is refused; mode stays."""
    bot, state = RecordingBot(), _fresh_state()
    await _tap_platform(bot, state, "spotify")
    download_mode.job_started(USER_A, download_mode.SPOTIFY)

    await _tap_platform(bot, state, "youtube")

    assert await _mode_of(state) == download_mode.SPOTIFY
    assert any("⏳" in (answer.text or "") for answer in bot.answers)


async def test_spotify_mode_unlocks_after_final_job() -> None:
    """Once the last Spotify job settles, the mode unlocks by itself."""
    bot, state = RecordingBot(), _fresh_state()
    await _tap_platform(bot, state, "spotify")
    download_mode.job_started(USER_A, download_mode.SPOTIFY)
    download_mode.job_settled(USER_A, download_mode.SPOTIFY)

    await _tap_platform(bot, state, "youtube")

    assert await _mode_of(state) == download_mode.YOUTUBE


# ---------------------------------------------------------------------------
# YouTube mode
# ---------------------------------------------------------------------------


async def test_none_to_youtube_on_platform_tap() -> None:
    """Tapping the YouTube section activates YouTube mode for that user."""
    bot, state = RecordingBot(), _fresh_state()

    await _tap_platform(bot, state, "youtube")

    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_youtube_mode_accepts_youtube_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A YouTube link inside YouTube mode flows into the intake (once)."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "youtube")

    await _send_url(bot, state, queue, YOUTUBE_1)

    assert seen["accepted"] == [YOUTUBE_1]
    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_youtube_mode_rejects_spotify_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Spotify link inside YouTube mode is refused; the mode is preserved."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "youtube")

    await _send_url(bot, state, queue, SPOTIFY_1)

    assert seen["accepted"] == []
    assert queue.tasks == []
    assert any("YouTube" in text for text in bot.texts)
    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_youtube_mode_accepts_multiple_youtube_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three YouTube links in a row — no reselecting the button in between."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "youtube")

    for url in (YOUTUBE_1, YOUTUBE_2, YOUTUBE_3):
        await _send_url(bot, state, queue, url)

    assert seen["accepted"] == [YOUTUBE_1, YOUTUBE_2, YOUTUBE_3]
    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_youtube_mode_unlocks_after_final_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no YouTube jobs left, the next link finds the mode unlocked."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "youtube")
    download_mode.job_started(USER_A, download_mode.YOUTUBE)
    download_mode.job_settled(USER_A, download_mode.YOUTUBE)

    await _send_url(bot, state, queue, YOUTUBE_1)

    assert seen["accepted"] == [YOUTUBE_1]
    assert await _mode_of(state) == download_mode.NONE


# ---------------------------------------------------------------------------
# Switching, callbacks, isolation, side effects
# ---------------------------------------------------------------------------


async def test_switch_youtube_to_spotify_to_youtube_while_busy() -> None:
    """YT active → SP refused; SP done → SP allowed → back to YT allowed."""
    bot, state = RecordingBot(), _fresh_state()
    await _tap_platform(bot, state, "youtube")
    download_mode.job_started(USER_A, download_mode.YOUTUBE)

    await _tap_platform(bot, state, "spotify")
    assert await _mode_of(state) == download_mode.YOUTUBE

    download_mode.job_settled(USER_A, download_mode.YOUTUBE)
    await _tap_platform(bot, state, "spotify")
    assert await _mode_of(state) == download_mode.SPOTIFY

    await _tap_platform(bot, state, "youtube")
    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_switch_spotify_to_youtube_to_spotify_while_busy() -> None:
    """SP active → YT refused; SP done → YT allowed → back to SP allowed."""
    bot, state = RecordingBot(), _fresh_state()
    await _tap_platform(bot, state, "spotify")
    download_mode.job_started(USER_A, download_mode.SPOTIFY)

    await _tap_platform(bot, state, "youtube")
    assert await _mode_of(state) == download_mode.SPOTIFY

    download_mode.job_settled(USER_A, download_mode.SPOTIFY)
    await _tap_platform(bot, state, "youtube")
    assert await _mode_of(state) == download_mode.YOUTUBE

    await _tap_platform(bot, state, "spotify")
    assert await _mode_of(state) == download_mode.SPOTIFY


async def test_stale_callback_rejected_after_mode_change() -> None:
    """A Spotify format tap that arrives after the switch to YouTube dies safe."""
    bot, state = RecordingBot(), _fresh_state(USER_A)
    queue = FakeQueue()
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(
        url=SPOTIFY_1,
        title="A Track",
        download_mode=download_mode.YOUTUBE,
    )

    await user_module.on_format_chosen(
        _callback(bot, "fmt:audio:m4a", USER_A),
        state,
        _user(USER_A),
        None,
        cast(Any, queue),
        cast(Bot, bot),
        lang=EN,
    )

    assert queue.tasks == []
    assert await _mode_of(state) == download_mode.YOUTUBE


async def test_two_users_have_independent_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Modes never leak between users — not the value, not the job count."""
    seen = _stub_intake_tail(monkeypatch)
    bot = RecordingBot()
    state_a, state_b = _fresh_state(USER_A), _fresh_state(USER_B)
    queue_a, queue_b = FakeQueue(), FakeQueue()
    await _tap_platform(bot, state_a, "spotify", USER_A)
    await _tap_platform(bot, state_b, "youtube", USER_B)
    download_mode.job_started(USER_A, download_mode.SPOTIFY)

    await _send_url(bot, state_b, queue_b, YOUTUBE_1, USER_B)
    await _send_url(bot, state_a, queue_a, YOUTUBE_1, USER_A)

    assert seen["accepted"] == [YOUTUBE_1]  # only user B's link went through
    assert await _mode_of(state_a) == download_mode.SPOTIFY
    assert await _mode_of(state_b) == download_mode.YOUTUBE


async def test_wrong_source_makes_no_queue_or_probe_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected link leaves nothing behind: no queue, no cache, no probe."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()
    await _tap_platform(bot, state, "spotify")

    await _send_url(bot, state, queue, YOUTUBE_1)

    assert queue.tasks == []
    assert seen["cached"] == []
    assert seen["probed"] == []


async def test_generic_mode_accepts_tiktok_rejects_spotify_and_youtube(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TikTok section takes TikTok links but neither Spotify nor YouTube."""
    seen = _stub_intake_tail(monkeypatch)
    bot, state, queue = RecordingBot(), _fresh_state(), FakeQueue()

    await _tap_platform(bot, state, "tiktok")

    assert await _mode_of(state) == download_mode.GENERIC
    await _send_url(bot, state, queue, TIKTOK_1)
    await _send_url(bot, state, queue, SPOTIFY_1)
    await _send_url(bot, state, queue, YOUTUBE_1)

    assert seen["accepted"] == []
    assert [task.url for task in queue.tasks] == [TIKTOK_1]
    assert await _mode_of(state) == download_mode.GENERIC
