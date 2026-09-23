"""The cookie-jar watcher: one alert per export, and only when it is news.

A replaced ``cookies.txt`` reaches the next download by itself, which is exactly
why nobody notices that it has not happened yet — and why an export with no
YouTube login (the mistake this project keeps rediscovering) stays invisible
until a download fails like a blocked IP. These tests pin when the admins get
told, what the message says, and that the loop survives shutdown and failures.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, cast

import pytest
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, User
from pydantic import ValidationError

from core.config import Settings
from handlers import admin as admin_module
from handlers.admin import on_alert_check
from handlers.admin import router as admin_router
from services import cookie_watch as cookie_watch_module
from services.cookie_watch import (
    DOCTOR_CALLBACK,
    REFRESH_CALLBACK,
    CookieJarWatcher,
    alert_keyboard,
    export_stamp,
    fresh_alert,
    notify_admins,
    regression_alert,
    run_cookie_watch,
    verify_export,
)
from services.doctor import DEFAULT_PROBE_URL
from services.extractor import CookieJarState, ExtractionError, ExtractorService, MediaInfo


def _state(root: Any, **overrides: Any) -> CookieJarState:
    """A jar state as ``ExtractorService.cookie_jar_state()`` would report it."""
    base = CookieJarState(
        path=root / "cookies.txt",
        kind="ok",
        size_bytes=512,
        exported_at=1_000.0,
        mount=None,
        writable=False,
        cookie_count=24,
        missing_login=(),
        copy_path=root / ".cookies" / "cookies.txt",
        copy_refreshed_at=900.0,
        in_sync=False,
    )
    return replace(base, **overrides)


class _SequenceExtractor:
    """``cookie_jar_state()`` hands out the queued states, then repeats the last.

    ``probe`` is what the verification's metadata extraction returns (or raises).
    """

    def __init__(self, *states: CookieJarState, probe: Any = None) -> None:
        self.states = list(states)
        self.calls = 0
        self.probe = probe
        self.probed_urls: list[str] = []

    def cookie_jar_state(self) -> CookieJarState:
        self.calls += 1
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    async def extract(self, url: str) -> MediaInfo:
        self.probed_urls.append(url)
        if isinstance(self.probe, BaseException):
            raise self.probe
        if self.probe is not None:
            return self.probe
        raise AttributeError("this fake was not given a probe result")


def _media(title: str = "Big Buck Bunny") -> MediaInfo:
    """What a metadata-only extraction returns (no bytes are fetched)."""
    return MediaInfo(
        source_url=DEFAULT_PROBE_URL,
        title=title,
        platform="YouTube",
        webpage_url=DEFAULT_PROBE_URL,
        extension="mp4",
        thumbnail=None,
        duration=600,
        filesize_approx=None,
        is_live=False,
    )


def _extractor(*states: CookieJarState, probe: Any = None) -> ExtractorService:
    """The watcher only asks for ``cookie_jar_state()``, so this stands in for one."""
    return cast(ExtractorService, _SequenceExtractor(*states, probe=probe))


def _watcher(*states: CookieJarState, interval_s: float = 60.0) -> CookieJarWatcher:
    return CookieJarWatcher(_extractor(*states), interval_s=interval_s)


# ---------------------------------------------------------------------------
# What counts as an export
# ---------------------------------------------------------------------------


def test_export_stamp_identifies_a_jar_file(tmp_path: Any) -> None:
    assert export_stamp(_state(tmp_path)) == (1_000.0, 512)


def test_a_missing_or_placeholder_directory_is_not_an_export(tmp_path: Any) -> None:
    for kind in ("missing", "directory"):
        state = _state(tmp_path, kind=kind, size_bytes=None, exported_at=None)
        assert export_stamp(state) is None, kind


# ---------------------------------------------------------------------------
# When the admins hear about it
# ---------------------------------------------------------------------------


def test_the_jar_present_at_startup_is_never_an_alert(tmp_path: Any) -> None:
    """A restart is not news — otherwise every deployed restart pings everyone."""
    watcher = _watcher(_state(tmp_path))
    baseline = watcher.prime()

    assert watcher.inspect(baseline) is None


def test_a_replaced_jar_that_was_not_loaded_alerts_once(tmp_path: Any) -> None:
    first = _state(tmp_path, exported_at=1_000.0)
    export = _state(tmp_path, exported_at=2_000.0, size_bytes=900, in_sync=False)
    watcher = _watcher(first, export)
    watcher.prime()

    alert = watcher.inspect()

    assert alert is not None
    assert alert.kind == "fresh"
    assert "cookies.txt" in alert.text and "24 کوکی" in alert.text
    assert "روی اکسپورت قبلی" in alert.text
    assert "دانلود بعدی" in alert.text
    # One message per export: the loop polls every interval, the jar does not change.
    assert watcher.inspect() is None


def test_a_second_export_alerts_again(tmp_path: Any) -> None:
    watcher = _watcher(
        _state(tmp_path, exported_at=1_000.0),
        _state(tmp_path, exported_at=2_000.0),
        _state(tmp_path, exported_at=2_000.0),  # unchanged: the polls in between
        _state(tmp_path, exported_at=3_000.0),
    )
    watcher.prime()

    assert watcher.inspect() is not None  # the 2_000 stamp
    assert watcher.inspect() is None
    assert watcher.inspect() is not None  # a *new* export is news again


def test_a_download_that_already_took_the_export_is_not_an_alert(tmp_path: Any) -> None:
    """The report the admins asked for is "not picked up yet", not "ever changed"."""
    watcher = _watcher(
        _state(tmp_path, exported_at=1_000.0),
        _state(tmp_path, exported_at=2_000.0, in_sync=True),
    )
    watcher.prime()

    assert watcher.inspect() is None


def test_a_jar_that_appeared_while_the_bot_ran_alerts(tmp_path: Any) -> None:
    """No copy exists yet, so "loaded" is still false — and downloads don't need one."""
    watcher = _watcher(
        _state(tmp_path, kind="missing", size_bytes=None, exported_at=None, in_sync=None),
        # A fresh process that never copied the jar: nothing was handed to yt-dlp,
        # so the state cannot say whether the new export was taken.
        _state(tmp_path, exported_at=2_000.0, in_sync=None),
    )
    watcher.prime()

    alert = watcher.inspect()

    assert alert is not None
    assert "هیچ کوکی‌ای نخوانده" in alert.text


def test_the_same_jar_is_not_re_announced_after_a_poll_of_no_change(tmp_path: Any) -> None:
    export = _state(tmp_path, exported_at=2_000.0)
    watcher = _watcher(_state(tmp_path, exported_at=1_000.0), export)
    watcher.prime()

    assert watcher.inspect() is not None
    for _ in range(5):
        assert watcher.inspect() is None


# ---------------------------------------------------------------------------
# What the message says
# ---------------------------------------------------------------------------


def test_a_new_export_without_a_youtube_login_says_so(tmp_path: Any) -> None:
    """The recurring failure: a jar that loads but signs nobody in."""
    text = fresh_alert(_state(tmp_path, missing_login=("LOGIN_INFO",)))

    assert "LOGIN_INFO" in text
    assert "export_cookies.py" in text
    assert "IP مسدود" in text  # names the misdiagnosis it prevents


def test_a_new_export_with_a_login_carries_no_warning(tmp_path: Any) -> None:
    text = fresh_alert(_state(tmp_path))

    assert "LOGIN_INFO" not in text
    assert "export_cookies.py" not in text  # nothing to fix, so nothing to suggest


def test_the_alert_says_where_the_jar_came_from(tmp_path: Any) -> None:
    text = fresh_alert(
        _state(tmp_path, writable=False, mount=None)  # a read-only file, no mount table
    )

    assert "فقط-خواندنی" in text


def test_a_jar_that_stops_being_usable_alerts_once(tmp_path: Any) -> None:
    watcher = _watcher(
        _state(tmp_path),
        _state(tmp_path, kind="unusable", exported_at=2_000.0),
    )
    watcher.prime()

    alert = watcher.inspect()

    assert alert is not None and alert.kind == "regression"
    assert "از کار افتاد" in alert.text
    assert watcher.inspect() is None


# ---------------------------------------------------------------------------
# Verifying the export before telling anyone
# ---------------------------------------------------------------------------


async def test_a_working_export_is_reported_as_accepted(tmp_path: Any) -> None:
    state = _state(tmp_path)
    extractor = _extractor(state, probe=_media("Big Buck Bunny"))

    verdict = await verify_export(extractor, state)

    assert verdict.startswith("✅")
    assert "Big Buck Bunny" in verdict
    assert "الان" in verdict  # a moment in time, not a promise


async def test_the_probe_is_metadata_only(tmp_path: Any) -> None:
    """A verification must not download anything — the alert is still just a hint."""
    state = _state(tmp_path)
    extractor = _extractor(state, probe=_media())

    await verify_export(extractor, state)

    assert cast(_SequenceExtractor, extractor).probed_urls == [DEFAULT_PROBE_URL]


async def test_a_blocked_probe_blames_the_missing_login(tmp_path: Any) -> None:
    state = _state(tmp_path, missing_login=("LOGIN_INFO",))
    extractor = _extractor(
        state, probe=ExtractionError("EXTRACTOR_BLOCKED", "Sign in to confirm you're not a bot")
    )

    verdict = await verify_export(extractor, state)

    assert verdict.startswith("⛔️")
    assert "LOGIN_INFO" in verdict
    assert "نه IP" in verdict


async def test_a_blocked_probe_with_a_real_login_blames_the_ip(tmp_path: Any) -> None:
    state = _state(tmp_path)
    extractor = _extractor(state, probe=ExtractionError("EXTRACTOR_BLOCKED", "blocked"))

    verdict = await verify_export(extractor, state)

    assert "IP" in verdict and "YTDLP_PROXY" in verdict


async def test_a_stale_session_is_called_transient(tmp_path: Any) -> None:
    state = _state(tmp_path)
    extractor = _extractor(state, probe=ExtractionError("SESSION_STALE", "reloaded"))

    verdict = await verify_export(extractor, state)

    assert verdict.startswith("⚠️") and "گذرا" in verdict


async def test_a_probe_that_cannot_run_still_reports_something(tmp_path: Any) -> None:
    """An unverified alert beats no alert; the probe must never raise."""
    state = _state(tmp_path)

    for probe in (RuntimeError("no network"), AttributeError("no extract")):
        extractor = _extractor(state, probe=probe)
        verdict = await verify_export(extractor, state)
        assert verdict.startswith("❓"), probe
        assert "/doctor" in verdict


async def test_a_probe_that_hangs_is_not_awaited_forever(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cookie_watch_module, "PROBE_TIMEOUT_S", 0.01)

    class Slow:
        async def extract(self, url: str) -> MediaInfo:
            await asyncio.sleep(5)
            raise AssertionError("should have timed out")

    verdict = await verify_export(cast(ExtractorService, Slow()), _state(tmp_path))

    assert verdict.startswith("❓") and "طول کشید" in verdict


def test_a_placeholder_directory_replacing_the_jar_alerts(tmp_path: Any) -> None:
    watcher = _watcher(
        _state(tmp_path),
        _state(tmp_path, kind="directory", size_bytes=None, exported_at=None),
    )
    watcher.prime()

    alert = watcher.inspect()

    assert alert is not None and "پوشه است" in alert.text


def test_removing_the_jar_on_purpose_is_not_an_alert(tmp_path: Any) -> None:
    """A fresh clone has no jar at all; deleting one is a choice, not a symptom."""
    watcher = _watcher(
        _state(tmp_path),
        _state(tmp_path, kind="missing", size_bytes=None, exported_at=None),
    )
    watcher.prime()

    assert watcher.inspect() is None


def test_the_regression_alert_names_the_broken_shape(tmp_path: Any) -> None:
    assert "پوشه است" in regression_alert(_state(tmp_path, kind="directory"))
    assert "قابل‌خواندنی" in regression_alert(_state(tmp_path, kind="unusable"))


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class FakeBot:
    """Just enough Bot for the alert: records messages, can refuse one chat."""

    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.messages: list[tuple[int, str, str | None, Any]] = []
        self.failing = set(failing)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any = None,
    ) -> None:
        if chat_id in self.failing:
            raise RuntimeError("bot was blocked by the user")
        self.messages.append((chat_id, text, parse_mode, reply_markup))


async def test_the_alert_reaches_every_admin_as_html() -> None:
    bot = FakeBot()

    delivered = await notify_admins(bot, [11, 22], "🍪 <b>کوکی</b>")  # type: ignore[arg-type]

    assert delivered == 2
    assert [message[0] for message in bot.messages] == [11, 22]
    assert all(message[2] == ParseMode.HTML for message in bot.messages)


async def test_the_alert_carries_the_check_now_button() -> None:
    bot = FakeBot()

    await notify_admins(bot, [11], "🍪", reply_markup=alert_keyboard())  # type: ignore[arg-type]

    markup = bot.messages[0][3]
    assert markup is not None
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["بررسی همین حالا", "♻️ اکسپورت دوباره"]
    assert [button.callback_data for button in buttons] == [DOCTOR_CALLBACK, REFRESH_CALLBACK]


async def test_one_unreachable_admin_does_not_silence_the_others() -> None:
    bot = FakeBot(failing=(22,))

    delivered = await notify_admins(bot, [11, 22, 33], "x")  # type: ignore[arg-type]

    assert delivered == 2
    assert [message[0] for message in bot.messages] == [11, 33]


async def test_an_alert_nobody_can_receive_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBot(failing=(11,))

    with caplog.at_level("WARNING"):
        delivered = await notify_admins(bot, [11], "x")  # type: ignore[arg-type]

    assert delivered == 0
    assert "reached no admin" in caplog.text


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def test_the_loop_tells_the_admins_once_and_stops_on_shutdown(tmp_path: Any) -> None:
    extractor = _extractor(
        _state(tmp_path, exported_at=1_000.0),
        _state(tmp_path, exported_at=2_000.0, in_sync=False),
        probe=_media(),
    )
    watcher = CookieJarWatcher(extractor, interval_s=0.01)
    watcher.prime()
    bot = FakeBot()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_cookie_watch(stop, bot, [7], watcher)  # type: ignore[arg-type]
    )
    for _ in range(200):  # ~2s of polls, then prove dedupe across several cycles
        if bot.messages:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert [message[0] for message in bot.messages] == [7]
    assert "کوکی" in bot.messages[0][1]
    # The alert was verified with a probe before it went out, through the very
    # extractor that reads the jar.
    assert "✅ تست با همین اکسپورت" in bot.messages[0][1]
    assert cast(_SequenceExtractor, extractor).probed_urls == [DEFAULT_PROBE_URL]
    # The loop, not just the builder: the button has to reach Telegram.
    assert bot.messages[0][3] is not None
    assert task.done() and task.exception() is None


async def test_shutdown_does_not_wait_out_the_interval(tmp_path: Any) -> None:
    watcher = _watcher(_state(tmp_path), interval_s=3600.0)
    watcher.prime()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_cookie_watch(stop, FakeBot(), [7], watcher)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    stop.set()

    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


async def test_a_failing_cycle_does_not_kill_the_watcher(tmp_path: Any) -> None:
    class Exploding:
        def cookie_jar_state(self) -> CookieJarState:
            raise RuntimeError("disk on fire")

    watcher = CookieJarWatcher(cast(ExtractorService, Exploding()), interval_s=0.01)
    stop = asyncio.Event()

    task = asyncio.create_task(
        run_cookie_watch(stop, FakeBot(), [7], watcher)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.05)

    assert not task.done(), "the watcher must survive a broken cycle"
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


async def test_the_watcher_is_off_when_the_interval_is_zero(tmp_path: Any) -> None:
    assert not CookieJarWatcher(_extractor(_state(tmp_path)), interval_s=0).enabled
    assert CookieJarWatcher(_extractor(_state(tmp_path))).enabled


# ---------------------------------------------------------------------------
# The alert's one button
# ---------------------------------------------------------------------------


def test_the_buttons_are_the_doctor_and_the_export() -> None:
    """An alert has two honest answers: check it, or replace the jar and check that."""
    markup = alert_keyboard()

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert [button.text for button in buttons] == ["بررسی همین حالا", "♻️ اکسپورت دوباره"]
    assert [button.callback_data for button in buttons] == [DOCTOR_CALLBACK, REFRESH_CALLBACK]


class _SpyCallback:
    """Just enough CallbackQuery for the guard: no message, only an answer."""

    def __init__(self, user_id: int) -> None:
        self.from_user = User(id=user_id, is_bot=False, first_name="probe")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


async def test_the_button_is_admin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """An alert can be forwarded; the doctor behind it must not travel with it."""
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setattr(admin_module, "get_settings", lambda: Settings(_env_file=None))  # type: ignore[call-arg]
    stranger = _SpyCallback(user_id=99)

    # The extractor is None on purpose: reaching the doctor would blow up here.
    await admin_module.on_alert_check(stranger, None, None, lang="fa")

    assert stranger.answers == [("⛔️ فقط ادمین می‌تونه.", True)]


async def test_the_button_data_is_what_the_admin_router_accepts() -> None:
    """Asked through aiogram's own filter evaluation, so the two cannot drift."""
    registered = [
        handler
        for handler in admin_router.callback_query.handlers
        if handler.callback is on_alert_check
    ]
    assert registered, "the button would do nothing"
    callback = CallbackQuery(
        id="1",
        from_user=User(id=1, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=DOCTOR_CALLBACK,
    )

    accepted, _ = await registered[0].check(callback)
    assert accepted
    # Any other button on the alert must not end up in the doctor handler.
    declined, _ = await registered[0].check(
        callback.model_copy(update={"data": "cookie:something-else"})
    )
    assert not declined


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_poll_interval_defaults_to_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).cookie_watch_interval_s == 60.0


def test_blank_and_zero_intervals_are_understood(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, COOKIE_WATCH_INTERVAL_S="").cookie_watch_interval_s == 60.0
    assert _settings(monkeypatch, COOKIE_WATCH_INTERVAL_S="0").cookie_watch_interval_s == 0


def test_an_absurd_interval_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, COOKIE_WATCH_INTERVAL_S="7200")


# ---------------------------------------------------------------------------
# The same export, for the fallback engine
# ---------------------------------------------------------------------------


def _signed_in_jar(tmp_path: Any) -> Any:
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tlogin-value\n"
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\tsapisid-value\n",
        encoding="utf-8",
    )
    return path


def test_a_fresh_export_is_also_written_for_the_fallback_engine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cobalt reads its own file, once, at startup — so a new jar is a new login
    there only after a restart, and the alert is where an admin learns that."""
    jar = _signed_in_jar(tmp_path)
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    note = cookie_watch_module.cobalt_cookie_note(settings, jar)

    assert "موتور جایگزین" in note
    assert "restart cobalt" in note
    assert (tmp_path / "cobalt" / "cookies.json").is_file()


def test_the_note_is_not_repeated_when_nothing_changed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second pass over the same jar must not ask for another restart."""
    jar = _signed_in_jar(tmp_path)
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))
    cookie_watch_module.cobalt_cookie_note(settings, jar)

    assert cookie_watch_module.cobalt_cookie_note(settings, jar) == ""


def test_no_settings_means_no_note(tmp_path: Any) -> None:
    """The loop is also run without settings (tests, and any caller that only
    wants the jar alert) — that must stay a no-op, not a crash."""
    assert cookie_watch_module.cobalt_cookie_note(None, _signed_in_jar(tmp_path)) == ""


def test_the_note_repeats_the_login_warning_for_the_generated_file(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An anonymous jar still improves cobalt's path (it retrieves the player),
    so it is written — with the same caveat the jar itself gets."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tFALSE\t0\tVISITOR_INFO1_LIVE\txyz\n",
        encoding="utf-8",
    )
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    note = cookie_watch_module.cobalt_cookie_note(settings, jar)

    assert "restart cobalt" in note
    assert "LOGIN_INFO" in note
