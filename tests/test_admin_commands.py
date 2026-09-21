"""The admin commands that answer the questions after ``/doctor``.

``/refresh`` (and the alert's button) re-exports the jar from a browser profile on
demand, ``/fixlogin`` walks through getting a real login into it, and ``/trend``
says whether the last fix reduced the failures. All three are admin-only, and the
on-demand export must be the *same* guarded path the automatic one uses — so these
tests drive real aiogram objects through a bot that records the API calls instead
of talking to Telegram.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core.config import Settings
from handlers import admin as admin_module
from services.cookie_refresh import RefreshOutcome
from services.cookie_watch import DOCTOR_CALLBACK, REFRESH_CALLBACK
from services.doctor import FallbackHealth
from services.login_wizard import JarDiagnosis
from services.telemetry import BlockDigest, FixMark, Trend, TrendDay


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, answers with a real Message.

    Binding aiogram's objects to it (``.as_``) means the handlers run their real
    code — ``message.answer``, ``edit_text``, ``callback.answer`` — without a
    network, and the assertions read what Telegram would have received.
    """

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
    def edits(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, EditMessageText)]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]


def _message(text: str, bot: RecordingBot, user_id: int = 1) -> Message:
    """A real aiogram message, bound to the recording bot so its calls land there."""
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=555, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, user_id: int = 1) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=data,
        message=_message("alert", bot),
    ).as_(cast(Bot, bot))


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command reads the settings through the module that owns them."""
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setattr(
        admin_module, "get_settings", lambda: Settings(_env_file=None)  # type: ignore[call-arg]
    )


def _stub_refresh(
    monkeypatch: pytest.MonkeyPatch, outcome: RefreshOutcome | None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake_refresh(
        settings: Any, extractor: Any, bot: Any, admins: Any, **kwargs: Any
    ) -> RefreshOutcome | None:
        calls.append(kwargs)
        return outcome

    monkeypatch.setattr(admin_module.cookie_refresh, "auto_refresh_jar", fake_refresh)
    return calls


# ---------------------------------------------------------------------------
# /refresh
# ---------------------------------------------------------------------------


async def test_refresh_is_admin_only() -> None:
    bot = RecordingBot()
    message = _message("/refresh edge:Default", bot, user_id=9)

    await admin_module.cmd_refresh(message, bot, object(), object())

    assert bot.texts == ["⛔️ فقط ادمین می‌تونه."]
    assert bot.edits == [], "and nothing was exported"


async def test_refresh_without_a_profile_explains_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feature is off and nothing was named: say how to name one."""
    calls = _stub_refresh(monkeypatch, None)
    bot = RecordingBot()
    message = _message("/refresh", bot)

    await admin_module.cmd_refresh(message, bot, object(), object())

    assert "/refresh edge:Default" in bot.texts[0]
    assert "COOKIE_AUTO_EXPORT" in bot.texts[0]
    assert calls == [], "nothing to read, so nothing is attempted"


async def test_refresh_runs_the_guarded_path_and_edits_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_refresh(monkeypatch, RefreshOutcome("replaced", "", cookies=31, verdict="✅ تست شد"))
    bot = RecordingBot()
    message = _message("/refresh edge:Default", bot)

    await admin_module.cmd_refresh(message, bot, object(), object())

    assert bot.texts == ["♻️ در حال خواندن کوکی از پروفایل…"], "the admin sees it started"
    assert bot.edits and "جار کوکی" in bot.edits[-1]
    assert "✅ تست شد" in bot.edits[-1], "the probe verdict comes back to the invoker"
    assert calls[0]["spec"] == "edge:Default"
    assert calls[0]["force"] is True, "a human asked: the cooldown must not swallow it"
    assert calls[0]["pool"] is not None, "the fix is recorded for /trend"


async def test_refresh_while_another_export_runs_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_refresh(monkeypatch, None)
    bot = RecordingBot()
    message = _message("/refresh edge:Default", bot)

    await admin_module.cmd_refresh(message, bot, object(), object())

    assert "در جریان است" in bot.edits[-1]


async def test_refresh_uses_the_configured_profile_when_no_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COOKIE_AUTO_EXPORT", "edge:Default")
    calls = _stub_refresh(monkeypatch, RefreshOutcome("unreachable", "دیده نشد"))
    bot = RecordingBot()
    message = _message("/refresh", bot)

    await admin_module.cmd_refresh(message, bot, object(), object())

    assert calls[0]["spec"] == "edge:Default"


# ---------------------------------------------------------------------------
# The alert's "♻️ اکسپورت دوباره" button
# ---------------------------------------------------------------------------


async def test_the_refresh_button_is_admin_only() -> None:
    bot = RecordingBot()
    cb = _callback(bot, REFRESH_CALLBACK, user_id=9)

    await admin_module.on_alert_refresh(cb, bot, object(), object())

    assert [answer.show_alert for answer in bot.answers] == [True], (
        "a forwarded alert must not export anything"
    )
    assert "⛔️" in (bot.answers[0].text or "")


async def test_the_refresh_button_runs_the_export_and_edits_the_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COOKIE_AUTO_EXPORT", "edge:Default")
    calls = _stub_refresh(monkeypatch, RefreshOutcome("replaced", "", cookies=31, verdict="✅ تست شد"))
    bot = RecordingBot()
    cb = _callback(bot, REFRESH_CALLBACK)

    await admin_module.on_alert_refresh(cb, bot, object(), object())

    assert [call.text for call in bot.answers] == ["♻️ در حال اکسپورت…"]
    assert bot.edits[0] == "♻️ در حال خواندن کوکی از پروفایل…", "the alert becomes the placeholder"
    assert "جار کوکی" in bot.edits[-1], "and then carries the outcome"
    assert calls[0]["spec"] == "edge:Default" and calls[0]["force"] is True


async def test_the_refresh_button_without_a_profile_tells_the_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_refresh(monkeypatch, None)
    bot = RecordingBot()
    cb = _callback(bot, REFRESH_CALLBACK)

    await admin_module.on_alert_refresh(cb, bot, object(), object())

    assert "پروفایلی تنظیم نشده" in (bot.answers[0].text or "")
    assert "/refresh edge:Default" in bot.texts[0]
    assert calls == []


async def test_the_button_data_is_what_this_handler_accepts() -> None:
    """Asked through aiogram's own filter evaluation, so the two cannot drift."""
    registered = [
        handler
        for handler in admin_module.router.callback_query.handlers
        if handler.callback is admin_module.on_alert_refresh
    ]
    assert registered, "the button would do nothing"
    bot = RecordingBot()

    accepted, _ = await registered[0].check(_callback(bot, REFRESH_CALLBACK))
    declined, _ = await registered[0].check(_callback(bot, DOCTOR_CALLBACK))

    assert accepted and not declined, "each button has exactly one handler"


# ---------------------------------------------------------------------------
# /trend
# ---------------------------------------------------------------------------


def _trend() -> Trend:
    return Trend(
        days=14,
        per_day=(
            TrendDay(day=date(2026, 9, 8), counts={"login": 4, "ip": 1}),
            TrendDay(day=date(2026, 9, 10), counts={"ip": 1}),
        ),
        overall=BlockDigest(window="14 روز گذشته", counts={"login": 4, "ip": 2}, top_host=None),
        last_fix=FixMark(
            at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            kind="cookie_jar",
            detail="edge:Default → 31 کوکی",
            local_day=date(2026, 9, 9),
            age_seconds=86_400 * 2,
        ),
        effects=(),
        too_soon=True,
    )


async def test_trend_is_admin_only() -> None:
    bot = RecordingBot()
    message = _message("/trend", bot, user_id=9)

    await admin_module.cmd_trend(message, object())

    assert bot.texts == ["⛔️ فقط ادمین می‌تونه."]


async def test_trend_prints_the_days_and_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_trend(pool: Any, *, days: int = 14, now: Any = None) -> Trend:
        return _trend()

    monkeypatch.setattr(admin_module, "build_trend", fake_trend)
    bot = RecordingBot()
    message = _message("/trend", bot)

    await admin_module.cmd_trend(message, object())

    text = bot.texts[0]
    assert "روند 14 روزهٔ شکست‌ها" in text
    assert "09-08" in text and "🔐لاگین 4" in text
    assert "🔧 جار کوکی: edge:Default → 31 کوکی" in text, "the fix is marked on its own day"


# ---------------------------------------------------------------------------
# /blocks
# ---------------------------------------------------------------------------


def _digest() -> BlockDigest:
    return BlockDigest(
        window="7 روز گذشته", counts={"login": 5}, top_host=("youtube.com", 5)
    )


def _health(monkeypatch: pytest.MonkeyPatch, health: FallbackHealth) -> list[dict[str, Any]]:
    """Stub the fallback section; its own wording is pinned in test_fallback_use."""
    calls: list[dict[str, Any]] = []

    async def fake_health(settings: Any, cobalt: Any, **kwargs: Any) -> FallbackHealth:
        calls.append({"cobalt": cobalt, **kwargs})
        return health

    monkeypatch.setattr(admin_module, "fallback_health", fake_health)
    return calls


async def test_blocks_is_admin_only() -> None:
    bot = RecordingBot()
    message = _message("/blocks", bot, user_id=9)

    await admin_module.cmd_blocks(message, object())

    assert bot.texts == ["⛔️ فقط ادمین می‌تونه."]


async def test_blocks_reads_the_failures_with_the_net_s_health_under_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A digest full of login failures means one thing while the safety net is
    serving those links and another when it is not — so the two share a message.
    """

    async def fake_digest(pool: Any, *, days: int = 7, now: Any = None) -> BlockDigest:
        return _digest()

    monkeypatch.setattr(admin_module, "build_digest", fake_digest)
    calls = _health(monkeypatch, FallbackHealth("auth", "https://api.cobalt.example", "v10"))
    bot = RecordingBot()
    message = _message("/blocks", bot)

    await admin_module.cmd_blocks(message, object(), object())

    text = bot.texts[0]
    assert "گزارش 7 روزهٔ شکست‌ها" in text
    assert "کل: 5 شکست" in text
    assert "🔴 نیازمند کلید احراز هویت" in text
    assert calls[0]["probe"] is False, "the digest does not spend a request on a probe"
    assert calls[0]["pool"] is not None, "but it still reads what was recorded"


async def test_blocks_works_with_no_fallback_client_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The command is injected the running client; a run without one still has to
    answer (the section says what could not be checked)."""

    async def fake_digest(pool: Any, *, days: int = 7, now: Any = None) -> BlockDigest:
        return _digest()

    monkeypatch.setattr(admin_module, "build_digest", fake_digest)
    _health(monkeypatch, FallbackHealth("off"))
    bot = RecordingBot()
    message = _message("/blocks", bot)

    await admin_module.cmd_blocks(message, object())

    assert "⚫️ خاموش" in bot.texts[0]


# ---------------------------------------------------------------------------
# /fixlogin
# ---------------------------------------------------------------------------


async def test_fixlogin_is_admin_only() -> None:
    bot = RecordingBot()
    message = _message("/fixlogin", bot, user_id=9)

    await admin_module.cmd_fixlogin(message, object())

    assert bot.texts == ["⛔️ فقط ادمین می‌تونه."]


async def test_fixlogin_explains_the_jar_and_the_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnosis = JarDiagnosis(path=None, kind="ok", cookies=24, missing_login=("LOGIN_INFO",))
    monkeypatch.setattr(admin_module.login_wizard, "diagnose", lambda extractor: diagnosis)
    monkeypatch.setattr(admin_module.login_wizard, "candidates", lambda: ())
    bot = RecordingBot()
    message = _message("/fixlogin", bot)

    await admin_module.cmd_fixlogin(message, object())

    text = bot.texts[0]
    assert "راهنمای ورود واقعی یوتیوب" in text
    assert "LOGIN_INFO" in text
    assert "scripts/fix_login.py" in text, "the steps say what to run"
    assert "HttpOnly" in text, "and the trap that keeps this failing"


def test_every_admin_command_is_registered() -> None:
    names = {handler.callback.__name__ for handler in admin_module.router.message.handlers}
    assert {"cmd_doctor", "cmd_blocks", "cmd_refresh", "cmd_trend", "cmd_fixlogin"} <= names
