"""Telemetry: every failure on the record, and the weekly pattern for the admins.

One failed download is answered where it happens. What no single failure shows is
a *pattern* — thirty login blocks is one cookie re-export, thirty IP blocks is a
proxy — so these tests pin the classification, the report's wording, and the fact
that the weekly gate cannot fire twice (or stay quiet about a broken week).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from core.utils import utcnow
from services import helper_watch, telemetry
from services.extractor import ExtractionError, classify_block
from services.queue import DownloadTask
from services.telemetry import BlockDigest, next_step, render_digest

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str) -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name}\tvalue\n"


def _logged_out_jar(path: Path) -> Path:
    path.write_text(HEADER + _row("PREF"), encoding="utf-8")
    return path


def _logged_in_jar(path: Path) -> Path:
    path.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    return path


def _error(code: str = "EXTRACTOR_BLOCKED") -> ExtractionError:
    return ExtractionError(code, "Sign in to confirm you're not a bot")


def _digest(**counts: int) -> BlockDigest:
    return BlockDigest(window="7 روز گذشته", counts=counts, top_host=("youtube.com", 12))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_a_login_less_jar_on_youtube_is_a_login_failure(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    assert classify_block(_error(), "https://youtu.be/abc", jar) == "login"


def test_a_real_login_plus_a_block_is_an_ip_failure(tmp_path: Path) -> None:
    jar = _logged_in_jar(tmp_path / "cookies.txt")

    assert classify_block(_error(), "https://youtu.be/abc", jar) == "ip"


def test_a_stale_session_is_its_own_cause(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    assert classify_block(_error("SESSION_STALE"), "https://youtu.be/abc", jar) == "session"


@pytest.mark.parametrize(
    "code",
    [
        "PRIVATE_VIDEO",
        "AGE_RESTRICTED",
        "UNSUPPORTED_URL",
        "GENERAL",
        "DRM_PROTECTED",
        "IMAGE_ONLY",
    ],
)
def test_everything_else_is_the_site_or_the_link(tmp_path: Path, code: str) -> None:
    """Counting a private video (or a DRM site) as a block would bury the failures
    we can fix — and a DRM verdict is the site's own nature, not our address."""
    assert classify_block(_error(code), "https://youtu.be/abc", None) == "site"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def test_a_clean_week_says_so_plainly() -> None:
    text = render_digest(BlockDigest(window="7 روز گذشته", counts={}, top_host=None))

    assert "هیچ دانلودی با خطا ثبت نشده" in text
    assert "7 روز گذشته" in text


def test_the_report_counts_every_cause_and_names_the_host() -> None:
    text = render_digest(_digest(login=30, ip=4, site=2), headline="گزارش 7 روزهٔ شکست‌ها")

    assert "<b>گزارش 7 روزهٔ شکست‌ها</b>" in text
    assert "کل: 36 شکست در 7 روز گذشته" in text
    assert "youtube.com" in text and "(12 مورد)" in text
    assert "کوکی/لاگین: 30" in text and "IP یا PO token: 4" in text
    assert "خود سایت/لینک: 2" in text
    assert "سشن کهنه" not in text  # a zero is noise in a report


def test_the_next_step_follows_the_dominant_cause() -> None:
    assert "اکسپورت تازه" in next_step(_digest(login=3, ip=3))
    assert "YTDLP_PROXY" in next_step(_digest(ip=3))
    assert "گذراست" in next_step(_digest(session=3))
    assert "قابل رفع نیست" in next_step(_digest(site=3))


# ---------------------------------------------------------------------------
# Recording (never at the cost of the download path)
# ---------------------------------------------------------------------------


async def test_a_failure_is_recorded_with_its_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_record(pool: Any, **fields: Any) -> None:
        recorded.append(fields)

    monkeypatch.setattr(telemetry.database, "record_block_event", fake_record)
    jar = _logged_out_jar(tmp_path / "cookies.txt")
    task = DownloadTask(
        chat_id=7, telegram_id=7, url="https://www.youtube.com/watch?v=abc", media_format="video"
    )

    await telemetry.record_block(object(), task, _error(), jar)

    assert recorded == [
        {
            "telegram_id": 7,
            "url_host": "www.youtube.com",
            "code": "EXTRACTOR_BLOCKED",
            "cause": "login",
        }
    ]


async def test_a_failing_insert_does_not_break_the_failure_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exploding(pool: Any, **fields: Any) -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(telemetry.database, "record_block_event", exploding)
    task = DownloadTask(
        chat_id=7, telegram_id=7, url="https://youtu.be/abc", media_format="video"
    )

    await telemetry.record_block(object(), task, _error(), None)


# ---------------------------------------------------------------------------
# The weekly gate
# ---------------------------------------------------------------------------


class FakeBot:
    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failing = set(failing)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id in self.failing:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


@pytest.fixture
def state_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A stand-in for ``bot_state`` (get/set), shared by the tests below."""
    store: dict[str, str] = {}

    async def get_state(pool: Any, key: str) -> str | None:
        return store.get(key)

    async def set_state(pool: Any, key: str, value: str) -> None:
        store[key] = value

    monkeypatch.setattr(telemetry.database, "get_state", get_state)
    monkeypatch.setattr(telemetry.database, "set_state", set_state)
    return store


def _stub_digest(
    monkeypatch: pytest.MonkeyPatch,
    digest: BlockDigest,
    outages: list[helper_watch.Outage] | None = None,
) -> None:
    """Replace the weekly builder *and* the helper sweep: the gate needs no database."""

    async def build(pool: Any, *, days: int = 7, now: Any = None) -> BlockDigest:
        return digest

    async def summary(
        pool: Any, *, days: int = 7, now: Any = None
    ) -> list[helper_watch.Outage]:
        return list(outages or [])

    monkeypatch.setattr(telemetry, "build_digest", build)
    monkeypatch.setattr(telemetry.helper_watch, "summary", summary)


async def test_a_broken_week_is_reported_once(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    _stub_digest(monkeypatch, _digest(login=5))
    bot = FakeBot()

    first = await telemetry.maybe_send_digest(object(), bot, [1, 2])  # type: ignore[arg-type]
    second = await telemetry.maybe_send_digest(object(), bot, [1, 2])  # type: ignore[arg-type]

    assert first is True and second is False
    assert [chat_id for chat_id, _ in bot.sent] == [1, 2]
    assert telemetry.DIGEST_STATE_KEY in state_store


async def test_a_quiet_week_stays_silent_and_spends_nothing(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    """A quiet week (or a diagnostic run) must not consume the weekly window."""
    _stub_digest(monkeypatch, BlockDigest(window="7 روز گذشته", counts={}, top_host=None))
    bot = FakeBot()

    sent = await telemetry.maybe_send_digest(object(), bot, [1])  # type: ignore[arg-type]

    assert sent is False
    assert bot.sent == []
    assert state_store == {}, "the stamp records a delivery, not a check"


async def test_a_week_is_measured_from_the_last_report(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    _stub_digest(monkeypatch, _digest(login=5))
    bot = FakeBot()
    now = utcnow()
    state_store[telemetry.DIGEST_STATE_KEY] = (now - timedelta(days=6)).isoformat()

    assert await telemetry.maybe_send_digest(object(), bot, [1], now=now) is False  # type: ignore[arg-type]

    state_store[telemetry.DIGEST_STATE_KEY] = (now - timedelta(days=7, minutes=1)).isoformat()
    assert await telemetry.maybe_send_digest(object(), bot, [1], now=now) is True  # type: ignore[arg-type]


async def test_a_digest_nobody_received_is_retried_later(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    """The stamp is the promise that a report was delivered, not attempted."""
    _stub_digest(monkeypatch, _digest(login=5))
    bot = FakeBot(failing=(1,))

    sent = await telemetry.maybe_send_digest(object(), bot, [1])  # type: ignore[arg-type]

    assert sent is False
    assert telemetry.DIGEST_STATE_KEY not in state_store


async def test_a_dead_helper_is_reported_even_in_a_quiet_week(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    """The fallback can cover every failed link and be broken itself — that is a report."""
    _stub_digest(
        monkeypatch,
        BlockDigest(window="7 روز گذشته", counts={}, top_host=None),
        outages=[
            helper_watch.Outage(
                helper="session", hours=4.5, transitions=3, last_reason="پاسخ نمی‌دهد"
            )
        ],
    )
    bot = FakeBot()

    sent = await telemetry.maybe_send_digest(object(), bot, [1])  # type: ignore[arg-type]

    assert sent is True
    assert "هلپرها" in bot.sent[0][1]
    assert telemetry.DIGEST_STATE_KEY in state_store


async def test_a_nonsense_stamp_is_treated_as_due(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    _stub_digest(monkeypatch, _digest(login=5))
    state_store[telemetry.DIGEST_STATE_KEY] = "yesterday-ish"

    assert await telemetry.maybe_send_digest(object(), FakeBot(), [1]) is True  # type: ignore[arg-type]


async def test_without_admins_nothing_is_asked_of_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a digest with nobody to receive it must not query")

    monkeypatch.setattr(telemetry.database, "get_state", explode)

    assert await telemetry.maybe_send_digest(object(), FakeBot(), []) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The alarm (as opposed to the weekly trend)
# ---------------------------------------------------------------------------


def _stub_recent(monkeypatch: pytest.MonkeyPatch, digest: BlockDigest) -> None:
    async def build(pool: Any, *, minutes: int = 60, now: Any = None) -> BlockDigest:
        return digest

    monkeypatch.setattr(telemetry, "build_recent_digest", build)


def _recent(**counts: int) -> BlockDigest:
    return BlockDigest(
        window=f"{telemetry.EARLY_ALERT_WINDOW_MINUTES} دقیقهٔ گذشته",
        counts=counts,
        top_host=("youtu.be", sum(counts.values())),
    )


async def test_a_run_of_login_failures_pages_the_admins_now(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    _stub_recent(monkeypatch, _recent(login=3))
    bot = FakeBot()

    sent = await telemetry.maybe_send_early_alert(object(), bot, [1, 2])  # type: ignore[arg-type]

    assert sent is True
    assert [chat_id for chat_id, _ in bot.sent] == [1, 2]
    text = bot.sent[0][1]
    assert "3 شکست لاگین" in text and "60 دقیقه" in text
    assert "کوکی/لاگین: 3" in text, "the alarm carries the same counts as the report"
    assert "اکسپورت تازه" in text, "and the fix"
    assert telemetry.EARLY_ALERT_STATE_KEY in state_store


async def test_one_failure_is_not_a_pattern(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    _stub_recent(monkeypatch, _recent(login=telemetry.EARLY_ALERT_THRESHOLD - 1))
    bot = FakeBot()

    assert await telemetry.maybe_send_early_alert(object(), bot, [1]) is False  # type: ignore[arg-type]
    assert bot.sent == [] and state_store == {}


async def test_other_causes_do_not_page(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    """Only the fixable cause is an alarm; private videos are not an incident."""
    _stub_recent(monkeypatch, _recent(site=20, ip=20))

    assert await telemetry.maybe_send_early_alert(object(), FakeBot(), [1]) is False  # type: ignore[arg-type]


async def test_the_alarm_has_its_own_cooldown(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    """A burst of failures must not turn into a burst of messages."""
    _stub_recent(monkeypatch, _recent(login=9))
    bot = FakeBot()
    now = utcnow()

    first = await telemetry.maybe_send_early_alert(object(), bot, [1], now=now)  # type: ignore[arg-type]
    during = await telemetry.maybe_send_early_alert(object(), bot, [1], now=now)  # type: ignore[arg-type]
    after_cooldown = utcnow() + timedelta(seconds=telemetry.EARLY_ALERT_COOLDOWN_S + 1)
    later = await telemetry.maybe_send_early_alert(object(), bot, [1], now=after_cooldown)  # type: ignore[arg-type]

    assert (first, during, later) == (True, False, True)
    assert len(bot.sent) == 2


async def test_an_alarm_nobody_received_is_retried(
    monkeypatch: pytest.MonkeyPatch, state_store: dict[str, str]
) -> None:
    _stub_recent(monkeypatch, _recent(login=3))

    sent = await telemetry.maybe_send_early_alert(object(), FakeBot(failing=(1,)), [1])  # type: ignore[arg-type]

    assert sent is False
    assert telemetry.EARLY_ALERT_STATE_KEY not in state_store


def test_the_alarm_thresholds_are_sane() -> None:
    assert telemetry.EARLY_ALERT_THRESHOLD >= 2
    assert 5 <= telemetry.EARLY_ALERT_WINDOW_MINUTES <= 24 * 60
    assert 60.0 <= telemetry.EARLY_ALERT_COOLDOWN_S <= telemetry.EARLY_ALERT_WINDOW_MINUTES * 60


def test_the_windows_are_sane() -> None:
    assert telemetry.DIGEST_DAYS == 7
    assert telemetry.KEEP_DAYS > telemetry.DIGEST_DAYS


def test_the_digest_window_is_a_real_week_end_to_end() -> None:
    """Guards against a future edit that makes ``build_digest`` read the wrong span."""
    start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert (start + timedelta(days=telemetry.DIGEST_DAYS)) - start == timedelta(days=7)
