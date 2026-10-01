"""Redis degraded-mode visibility (P1-3): said out loud, throttled, to admins.

A boot that wanted Redis but fell back to memory queue + FSM keeps serving
(fail-open) — but queued jobs and dialog state die with the process, and until
now only a log line said so. These tests pin the three visible consequences:
a /doctor row, one throttled admin notice, and nothing for ordinary users.
The throttle lives in the shared ``bot_state`` table (faked here), never in
process memory — a restart inside the window stays silent. Mid-run Redis
death is intentionally untested here: it is reported, not redesigned.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import main as entrypoint
from handlers import admin as admin_module
from services.doctor import Check, DoctorReport, storage_check

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
ADMINS = (11, 22)


class _FakePool:
    """The ``bot_state`` table as a dict — shared across calls like Postgres."""

    def __init__(self, state: dict[str, str] | None = None) -> None:
        self.state = state if state is not None else {}

    async def fetchval(self, _query: str, key: str) -> str | None:
        return self.state.get(key)

    async def execute(self, _query: str, key: str, value: str) -> None:
        self.state[key] = value


class _FailingPool(_FakePool):
    async def fetchval(self, _query: str, key: str) -> str | None:
        raise ConnectionError("database unreachable")

    async def execute(self, _query: str, key: str, value: str) -> None:
        raise ConnectionError("database unreachable")


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def _report() -> DoctorReport:
    return DoctorReport(
        checks=(Check("یوتیوب", "ok", "سالم است"),),
        verdict="مسیر دانلود سالم است",
        next_step="کاری لازم نیست",
    )


# ---------------------------------------------------------------------------
# The notice: once per window, admins only, honest wording
# ---------------------------------------------------------------------------


async def test_healthy_boot_sends_nothing_and_touches_no_state() -> None:
    bot, pool = _FakeBot(), _FakePool()

    sent = await entrypoint.maybe_notify_storage_degraded(
        bot, pool, ADMINS, degraded=False, now=NOW  # type: ignore[arg-type]
    )

    assert sent is False
    assert bot.sent == []
    assert pool.state == {}


async def test_degraded_boot_notifies_each_admin_once() -> None:
    bot, pool = _FakeBot(), _FakePool()

    sent = await entrypoint.maybe_notify_storage_degraded(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )

    assert sent is True
    assert [chat for chat, _text in bot.sent] == [11, 22]
    assert entrypoint._STORAGE_DEGRADED_STATE_KEY in pool.state


async def test_degraded_notice_names_the_restart_cost_not_the_url() -> None:
    bot, pool = _FakeBot(), _FakePool()
    await entrypoint.maybe_notify_storage_degraded(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )

    text = bot.sent[0][1]
    assert "ری‌استارت" in text
    assert "حافظه" in text
    # The Redis URL can carry a password: no URL of any kind in the notice.
    assert "://" not in text


async def test_repeated_degraded_boots_are_throttled_by_shared_state() -> None:
    pool = _FakePool()
    first, second = _FakeBot(), _FakeBot()

    assert await entrypoint.maybe_notify_storage_degraded(
        first, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    ) is True
    # A restart minutes later reads the same bot_state row — silent, and the
    # silence comes from shared state, not from this process remembering.
    assert await entrypoint.maybe_notify_storage_degraded(
        second, pool, ADMINS, degraded=True, now=NOW + timedelta(minutes=5)  # type: ignore[arg-type]
    ) is False

    assert len(first.sent) == 2
    assert second.sent == []


async def test_the_notice_returns_after_the_window() -> None:
    pool = _FakePool()
    bot = _FakeBot()
    await entrypoint.maybe_notify_storage_degraded(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )

    later = _FakeBot()
    sent = await entrypoint.maybe_notify_storage_degraded(
        later, pool, ADMINS, degraded=True, now=NOW + timedelta(hours=7)  # type: ignore[arg-type]
    )

    assert sent is True
    assert len(later.sent) == 2


async def test_recovery_sends_no_notice_and_keeps_the_stamp() -> None:
    pool = _FakePool()
    bot = _FakeBot()
    await entrypoint.maybe_notify_storage_degraded(
        bot, pool, ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )
    stamped = dict(pool.state)

    recovered = _FakeBot()
    sent = await entrypoint.maybe_notify_storage_degraded(
        recovered, pool, ADMINS, degraded=False, now=NOW + timedelta(hours=7)  # type: ignore[arg-type]
    )

    assert sent is False
    assert recovered.sent == []
    assert pool.state == stamped, "recovery is silent — and must not reset the window"


async def test_ordinary_users_get_nothing() -> None:
    bot, pool = _FakeBot(), _FakePool()
    await entrypoint.maybe_notify_storage_degraded(
        bot, pool, (11,), degraded=True, now=NOW  # type: ignore[arg-type]
    )

    assert {chat for chat, _text in bot.sent} == {11}


async def test_a_dead_database_still_boots_without_a_notice() -> None:
    bot = _FakeBot()

    sent = await entrypoint.maybe_notify_storage_degraded(
        bot, _FailingPool(), ADMINS, degraded=True, now=NOW  # type: ignore[arg-type]
    )

    assert sent is False
    assert bot.sent == []


# ---------------------------------------------------------------------------
# /doctor: the degraded row rides along, never flips the diagnosis
# ---------------------------------------------------------------------------


def test_storage_check_states() -> None:
    degraded = storage_check(True)
    healthy = storage_check(False)

    assert degraded.status == "warn"
    assert "از بین می‌روند" in degraded.detail
    assert degraded.section is True
    assert healthy.status == "ok"


async def test_doctor_reports_the_degraded_row_without_flipping_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _doctor(*args: Any, **kwargs: Any) -> DoctorReport:
        return _report()

    monkeypatch.setattr(admin_module, "run_youtube_doctor", _doctor)
    bot = _FakeBot()

    report = await admin_module.run_doctor_into(
        bot, 11, extractor=None, storage_degraded=True  # type: ignore[arg-type]
    )

    assert report is not None
    assert report.healthy is True, "a warn row must not flip the YouTube diagnosis"
    assert report.checks[-1].name == "صف و حافظه"
    assert report.checks[-1].status == "warn"
    assert "از بین می‌روند" in report.checks[-1].detail
    assert "حکم: مسیر دانلود سالم است" in report.render()
    assert len(bot.sent) == 1 and bot.sent[0][0] == 11


async def test_doctor_without_the_flag_is_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _doctor(*args: Any, **kwargs: Any) -> DoctorReport:
        return _report()

    monkeypatch.setattr(admin_module, "run_youtube_doctor", _doctor)

    report = await admin_module.run_doctor_into(
        _FakeBot(), 11, extractor=None  # type: ignore[arg-type]
    )

    assert report is not None
    assert [check.name for check in report.checks] == ["یوتیوب"]
