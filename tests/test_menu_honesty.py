"""The honest menu: what may be offered, and what a failed lookup never becomes.

The two production bugs pinned here: a video link whose qualities could not be
discovered used to silently offer the default download under an empty quality
screen — and the audio grid used to blur "the source has this" with "ffmpeg can
make this". So the contract under test is the menu↔delivery one at the *flow*
level: only discovered rungs are offered, a failed capability lookup says so and
offers a retry (which re-extracts), the automatic row exists only when an
operator deliberately enabled it and never pretends to be an exact quality, and
native formats are labelled as the source's own while re-encodes are labelled as
the conversion targets they are. All offline: probes and resolvers are doubles.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.config import Settings
from core.i18n import t
from handlers import user as user_module
from handlers.user import DownloadStates
from services.delivery import produced_quality_label
from services.extractor import (
    AudioCapability,
    MediaInfo,
    VideoOption,
    audio_capability,
)

USER_ID = 4242
FA = "fa"
EN = "en"
YOUTUBE = "https://youtu.be/abc"
SPOTIFY = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, answers with a real Message."""

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
    def screens(self) -> list[str]:
        return [
            call.text or ""
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
        ]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    @property
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
            and call.reply_markup is not None
        ]


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
        message=_message("menu", bot),
    ).as_(cast(Bot, bot))


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _state(url: str = YOUTUBE) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


def _user() -> dict[str, Any]:
    return {
        "telegram_id": USER_ID,
        "username": "u",
        "language": FA,
        "is_premium": False,
        "premium_until": None,
    }


class _FakeQueue:
    def __init__(self) -> None:
        self.tasks: list[Any] = []

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return len(self.tasks)


class _FakeExtractor:
    """Answers the metadata probe with one canned :class:`MediaInfo`."""

    def __init__(self, info: MediaInfo) -> None:
        self.info = info
        self.urls: list[str] = []

    async def extract(self, url: str) -> MediaInfo:
        self.urls.append(url)
        return self.info


def _info(options: tuple[VideoOption, ...]) -> MediaInfo:
    return MediaInfo(
        source_url=YOUTUBE,
        title="A Clip",
        platform="youtube",
        webpage_url=YOUTUBE,
        extension="mp4",
        thumbnail=None,
        duration=95,
        filesize_approx=1,
        is_live=False,
        video_options=options,
    )


class _NoRefusal:
    """``preflight`` with nothing to say (the real rules have their own tests)."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return type("Verdict", (), {"refused": False, "message": ""})()


@pytest.fixture(autouse=True)
def _quiet_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No database, no cache, no preflight, no tap history — the menu is what is
    under test."""

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 0, "last_download_date": ""}

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "_probe_supported", supported)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    user_module._recent_requests.clear()


# ---------------------------------------------------------------------------
# A failed capability lookup never becomes a default download
# ---------------------------------------------------------------------------


async def test_a_crafted_default_tap_on_a_failed_lookup_is_refused() -> None:
    """Case 3+7: with no ladder discovered and no automatic row drawn, a crafted
    ``fmt:video:best`` is data — and it buys nothing."""
    bot = RecordingBot()
    state = _state()
    queue = _FakeQueue()

    await user_module._queue_url_flow(
        _message(YOUTUBE, bot),
        state,
        _user(),
        YOUTUBE,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=cast(Any, queue),
    )
    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:best"),
        state,
        _user(),
        object(),
        cast(Any, queue),
        bot,
        lang=FA,
    )

    assert queue.tasks == [], "no silent default download"
    assert bot.answers and bot.answers[-1].show_alert is True


async def test_the_automatic_row_exists_only_when_deliberately_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in (MENU_AUTO_BEST=1) draws one clearly *automatic* row — never an
    exact quality — and only then does its tap run."""

    def settings() -> Settings:
        return Settings(_env_file=None, MENU_AUTO_BEST="1")  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(user_module, "get_settings", settings)
    bot = RecordingBot()
    state = _state()
    queue = _FakeQueue()

    await user_module._queue_url_flow(
        _message(YOUTUBE, bot),
        state,
        _user(),
        YOUTUBE,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=cast(Any, queue),
    )

    rows = _buttons(bot.keyboards[-1])
    auto = [data for label, data in rows if label == t("intake.auto_best_btn", FA)]
    assert auto == ["fmt:video:best"], "labelled as an automatic pick"
    assert all(run not in t("intake.auto_best_btn", FA) for run in ("360", "720", "1080")), (
        "the label claims no resolution — it is an automatic pick, not an exact quality"
    )

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:best"),
        state,
        _user(),
        object(),
        cast(Any, queue),
        bot,
        lang=FA,
    )
    assert [task.quality for task in queue.tasks] == ["best"]


async def test_the_retry_button_re_extracts_and_can_find_the_ladder() -> None:
    """The retry is not a nicety: it runs the capability lookup again — and a
    menu that could not be drawn before is drawn from the fresh probe."""

    class _Bot(RecordingBot):
        state: Any = None

    bot = _Bot()
    state = _state()
    await user_module._queue_url_flow(
        _message(YOUTUBE, bot),
        state,
        _user(),
        YOUTUBE,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=cast(Any, _FakeQueue()),
    )
    assert t("intake.probe_failed", FA) in bot.screens[-1], "first lookup failed"

    from types import SimpleNamespace

    extractor = _FakeExtractor(_info((VideoOption(720, 8 * 1024 * 1024, True),)))
    bot.state = SimpleNamespace(extractor=extractor)

    await user_module.on_probe_retry(
        _callback(bot, user_module.PROBE_CALLBACK),
        state,
        _user(),
        object(),
        cast(Any, _FakeQueue()),
        bot,
        lang=FA,
    )

    assert extractor.urls == [YOUTUBE], "the probe ran again"
    assert ("720p · 8 MB", "fmt:video:720") in _buttons(bot.keyboards[-1])


async def test_a_retry_tap_on_a_dead_screen_gets_the_clear_retry_message() -> None:
    """Stale selection → the clear answer every dead menu gets: send the link
    again (which re-runs the probe)."""
    bot = RecordingBot()

    await user_module.on_stale_probe_tap(_callback(bot, user_module.PROBE_CALLBACK), lang=FA)

    assert bot.answers and bot.answers[0].show_alert is True
    assert t("intake.stale", FA) in (bot.answers[0].text or "")


async def test_a_stale_media_tap_says_how_to_retry_and_changes_nothing() -> None:
    bot = RecordingBot()
    queue = _FakeQueue()

    await user_module.on_stale_media_tap(
        _callback(bot, "fmt:video:1080"), _user(), lang=FA,
    )

    assert queue.tasks == []
    assert bot.answers and t("intake.stale", FA) in (bot.answers[0].text or "")


# ---------------------------------------------------------------------------
# Spotify: a resolver in front of the menu is said out loud — or fails honestly
# ---------------------------------------------------------------------------


async def test_an_unresolvable_spotify_track_gets_an_honest_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 6: the resolver failing is a localized message and a retry — never a
    menu of formats no source has been found to deliver."""

    async def broken(url: str, **kwargs: Any) -> Any:
        raise RuntimeError("no network")

    monkeypatch.setattr(user_module.spotify, "lookup", broken)
    bot = RecordingBot()
    queue = _FakeQueue()

    await user_module._queue_url_flow(
        _message(SPOTIFY, bot),
        _state(),
        _user(),
        SPOTIFY,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=cast(Any, queue),
    )

    assert t("intake.spotify_unresolved", FA) in bot.screens[-1]
    assert (t("intake.probe_retry_btn", FA), user_module.PROBE_CALLBACK) in _buttons(
        bot.keyboards[-1]
    )
    assert queue.tasks == []


async def test_a_resolved_spotify_track_says_where_the_audio_comes_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    async def lookup(url: str, **kwargs: Any) -> Any:
        return SimpleNamespace(duration_s=150.0)

    monkeypatch.setattr(user_module.spotify, "lookup", lookup)
    bot = RecordingBot()

    await user_module._queue_url_flow(
        _message(SPOTIFY, bot),
        _state(),
        _user(),
        SPOTIFY,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=cast(Any, _FakeQueue()),
    )

    assert t("intake.spotify_note", FA) in bot.screens[-1], (
        "the DRM/counterpart reality is stated before the menu is drawn"
    )
    assert ("🎧 MP3", "audf:mp3") in _buttons(bot.keyboards[-1])


# ---------------------------------------------------------------------------
# Audio capability: native vs converted vs undeliverable (cases 4 and 5)
# ---------------------------------------------------------------------------


def test_only_pipeline_deliverable_formats_are_offered() -> None:
    """Case 4: without ffmpeg the untouched stream is the one promise left; a
    lossless file the transport would refuse is no option at all."""
    capability = audio_capability(
        source_ext="m4a", duration_s=150.0, has_ffmpeg=False, upload_limit_bytes=0
    )
    assert capability.formats == ("m4a",)

    capability = audio_capability(
        source_ext="m4a", duration_s=3600.0, has_ffmpeg=True, upload_limit_bytes=1024
    )
    assert capability.formats == ("mp3", "m4a", "opus"), (
        "wav/flac would blow the transport ceiling and are simply absent"
    )


def test_native_and_converted_formats_are_modelled_apart() -> None:
    """Case 5, model side: a format the source *has* is never confused with a
    format ffmpeg can merely *make*."""
    native_source = audio_capability(source_ext="m4a", has_ffmpeg=True)
    assert native_source.native == ("m4a",)
    assert native_source.converted == ("mp3", "flac", "opus", "wav")
    assert native_source.is_native("m4a") and not native_source.is_converted("m4a")

    foreign_source = audio_capability(source_ext="webm", has_ffmpeg=True)
    assert foreign_source.native == ()
    assert "m4a" in foreign_source.converted, (
        "on a webm source m4a only exists as a re-encode"
    )
    assert foreign_source.copy_ok is False

    undeliverable = audio_capability(source_ext="m4a", has_ffmpeg=False)
    assert "flac" not in undeliverable.formats, (
        "a format nobody can deliver is absent, not flagged"
    )


def test_native_and_converted_formats_are_labelled_apart() -> None:
    """Case 5, label side: the untouched row names itself «Original», a re-encode
    row names its *encoding target* — and the finished file's caption agrees."""
    rows = dict(
        (data, label) for label, data in user_module._level_rows("m4a", EN, duration=150)
    )
    original = rows[user_module._fmt_callback("audio", "m4a")]
    assert "Original" in original and "kbps" not in original
    for tier in ("m4a.high", "m4a.balanced", "m4a.small"):
        label = rows[user_module._fmt_callback("audio", tier)]
        assert "kbps" in label and "Original" not in label, label

    assert produced_quality_label("audio", "m4a", ".m4a", EN) == "M4A · Original"
    converted = produced_quality_label("audio", "mp3.best", ".mp3", EN)
    assert converted == "MP3 · 320 kbps" and "Original" not in converted, (
        "a converted MP3 is never called source-native"
    )


async def test_the_level_screen_says_a_rate_row_is_a_conversion_target() -> None:
    bot = RecordingBot()
    state = _state("https://soundcloud.com/a/b")
    await state.set_state(DownloadStates.waiting_format)
    await state.update_data(
        url="https://soundcloud.com/a/b",
        title="",
        duration=0,
        audio_offered=["mp3"],
        copy_ok=True,
    )

    await user_module.on_audio_format(_callback(bot, "audf:mp3"), state, lang=FA)

    assert t("audio.converted_note", FA) in bot.screens[-1]
    assert ("💎 320 kbps", "fmt:audio:mp3.best") in _buttons(bot.keyboards[-1])


def test_the_audio_grid_is_the_capability_not_the_global_catalogue() -> None:
    """Only what this source can become gets a button — the catalogue is not the
    menu."""
    markup = user_module._question_keyboard(
        "https://soundcloud.com/a/b",
        EN,
        capability=AudioCapability(
            formats=("m4a",), copy_ok=True, native=("m4a",), converted=()
        ),
    )
    assert [data for _, data in _buttons(markup)] == ["audf:m4a", "menu:download"], (
        "one deliverable format (its presets one tap deeper) and the way out; "
        "nothing the pipeline cannot finish"
    )
