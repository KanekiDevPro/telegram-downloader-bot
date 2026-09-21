"""``/trend``: failures per day, and whether the last fix actually reduced them.

The honest part is the comparison. A fix from yesterday has one day after it and
four before, so counting raw failures would call every recent fix an improvement —
the trend compares *daily rates*, refuses to judge a fix that is minutes old, and
marks the fix on its own day even when that day was quiet. Those are the rules
these tests pin, with the database faked out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from services import telemetry

FIX_AT = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)  # two days after the fix


class FakeDb:
    """The four queries ``build_trend`` makes, answered from a dict."""

    def __init__(self) -> None:
        self.per_day: list[tuple[date, str, int]] = []
        self.before: dict[str, int] = {}
        self.after: dict[str, int] = {}
        self.fix: tuple[datetime, str, str] | None = None
        self.zone_seen: str | None = None

    async def blocks_per_day(
        self, pool: Any, since: datetime, timezone_name: str
    ) -> list[tuple[date, str, int]]:
        self.zone_seen = timezone_name
        return self.per_day

    async def block_counts(
        self, pool: Any, since: datetime, until: datetime | None = None
    ) -> dict[str, int]:
        if self.fix is None:
            # No fix to weigh: the whole window is just the days we were given.
            totals: dict[str, int] = {}
            for _, cause, count in self.per_day:
                totals[cause] = totals.get(cause, 0) + count
            return totals
        return self.before if until == self.fix[0] else self.after

    async def top_block_host(
        self, pool: Any, since: datetime, until: datetime | None = None
    ) -> tuple[str, int] | None:
        return None

    async def latest_fix_event(
        self, pool: Any, kind: str | None = None
    ) -> tuple[datetime, str, str] | None:
        return self.fix


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    fake = FakeDb()
    for name in ("blocks_per_day", "block_counts", "top_block_host", "latest_fix_event"):
        monkeypatch.setattr(telemetry.database, name, getattr(fake, name))
    return fake


async def build(days: int = 14) -> telemetry.Trend:
    return await telemetry.build_trend(object(), days=days, now=NOW)


# ---------------------------------------------------------------------------
# The days
# ---------------------------------------------------------------------------


async def test_the_days_come_from_the_database_grouped_by_cause(db: FakeDb) -> None:
    db.per_day = [
        (date(2026, 9, 8), "login", 4),
        (date(2026, 9, 8), "ip", 1),
        (date(2026, 9, 10), "site", 1),
    ]

    trend = await build()

    assert [day.day for day in trend.per_day] == [date(2026, 9, 8), date(2026, 9, 10)]
    assert trend.per_day[0].total == 5 and trend.per_day[0].count("login") == 4
    assert trend.quiet_days == 12, "a quiet day is the healthy state"


async def test_the_days_are_grouped_in_the_configured_timezone(
    db: FakeDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """\"The day the fix landed\" has to mean the operator's day, not UTC's.

    ``ZoneInfo`` is stubbed here: whether *this* machine has a tz database is not
    the question (the next test covers the fallback).
    """
    monkeypatch.setattr(telemetry, "ZoneInfo", lambda name: timezone.utc)

    await telemetry.build_trend(object(), now=NOW, timezone_name="Asia/Tehran")

    assert db.zone_seen == "Asia/Tehran"


async def test_an_unknown_timezone_falls_back_to_utc(db: FakeDb) -> None:
    await telemetry.build_trend(object(), now=NOW, timezone_name="Mars/Olympus")

    assert db.zone_seen == "UTC"


def test_a_day_lists_its_causes_shortly() -> None:
    row = telemetry._day_row(
        telemetry.TrendDay(day=date(2026, 9, 8), counts={"login": 4, "ip": 1})
    )

    assert row.startswith("09-08  5 شکست")
    assert "🔐لاگین 4" in row and "🌐IP 1" in row
    assert "سایت" not in row, "a cause with no failures is not a column"


# ---------------------------------------------------------------------------
# The fix and its effect
# ---------------------------------------------------------------------------


async def test_a_fix_is_marked_on_its_own_day_even_when_that_day_was_quiet(
    db: FakeDb,
) -> None:
    db.per_day = [(date(2026, 9, 8), "login", 5), (date(2026, 9, 12), "ip", 1)]
    db.fix = (FIX_AT, "cookie_jar", "edge:Default → 31 کوکی")

    rendered = telemetry.render_trend(await build())

    marker = "── 🔧 جار کوکی: edge:Default → 31 کوکی ──"
    assert marker in rendered
    assert rendered.index("09-08") < rendered.index(marker) < rendered.index("09-12")


async def test_a_fix_is_weighed_by_daily_rates_not_raw_counts(db: FakeDb) -> None:
    """Four days before, two after: the *rates* are what decide the verdict."""
    db.fix = (FIX_AT, "cookie_jar", "edge:Default → 31 کوکی")
    db.before = {"login": 12, "ip": 2}
    db.after = {"login": 1, "ip": 2}

    trend = await build()
    login, everything = trend.effects

    assert (login.before, login.after) == (12, 1)
    assert login.before_days == pytest.approx(2.0) and login.after_days == pytest.approx(2.0)
    assert login.before_rate == pytest.approx(6.0) and login.after_rate == pytest.approx(0.5)
    assert login.verdict == "✅ محسوس کمتر شد"
    assert (everything.before, everything.after) == (14, 3)


async def test_a_fix_that_ended_the_failures_says_so(db: FakeDb) -> None:
    db.fix = (FIX_AT, "cookie_jar", "edge:Default → 31 کوکی")
    db.before = {"login": 8}
    db.after = {"login": 0}

    trend = await build()

    assert trend.effects[0].verdict == "✅ کامل قطع شد"


async def test_a_fix_that_did_not_help_says_that_too(db: FakeDb) -> None:
    """The reason to keep this report honest: the next step has to change."""
    db.fix = (FIX_AT, "cookie_jar", "edge:Default → 31 کوکی")
    db.before = {"ip": 4}
    db.after = {"ip": 9}

    trend = await build()

    assert trend.effects[0].verdict == "➖ قبل از اصلاح هم موردی از این نوع نبود"
    assert trend.effects[1].verdict == "⚠️ کمتر نشد — علت یا قدم بعدی چیز دیگری است"


async def test_a_fresh_fix_is_not_judged_yet(db: FakeDb) -> None:
    """Ten quiet minutes after an export is not evidence that it worked."""
    db.fix = (NOW - timedelta(minutes=10), "cookie_jar", "edge:Default → 31 کوکی")
    db.before = {"login": 30}
    db.after = {"login": 0}

    trend = await build()
    rendered = telemetry.render_trend(trend)

    assert trend.too_soon is True and trend.effects == ()
    assert "برای داوری زود است" in rendered
    assert "کامل قطع شد" not in rendered, "no verdict is invented"


async def test_a_fix_older_than_the_window_has_nothing_to_compare(db: FakeDb) -> None:
    db.fix = (NOW - timedelta(days=40), "cookie_jar", "edge:Default → 31 کوکی")

    trend = await build()

    assert trend.too_soon is True and trend.effects == ()
    assert trend.last_fix is not None, "it still shows, as a marker"


async def test_without_a_fix_the_trend_ranks_the_causes_instead(db: FakeDb) -> None:
    db.per_day = [(date(2026, 9, 8), "ip", 6)]

    rendered = telemetry.render_trend(await build())

    assert "🔧" not in rendered
    assert "قدم بعدی: بیشترین علت IP است" in rendered


# ---------------------------------------------------------------------------
# The quiet report, and rendering
# ---------------------------------------------------------------------------


async def test_a_quiet_window_is_still_a_report(db: FakeDb) -> None:
    rendered = telemetry.render_trend(await build())

    assert "هیچ شکستی ثبت نشده" in rendered


async def test_the_row_says_a_day_was_clean_before_a_later_failure(db: FakeDb) -> None:
    """Days with no failures do not need a row — but a marked day does."""
    db.per_day = [(date(2026, 9, 12), "login", 1)]
    db.fix = (FIX_AT, "cookie_jar", "edge:Default → 31 کوکی")

    rendered = telemetry.render_trend(await build())

    assert "(13 روز بدون شکست)" in rendered
    assert "09-12" in rendered


# ---------------------------------------------------------------------------
# Recording the fix (what makes any of the above possible)
# ---------------------------------------------------------------------------


async def test_recording_a_fix_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[tuple[str, str]] = []

    async def record(pool: Any, *, kind: str, detail: str = "") -> None:
        recorded.append((kind, detail))

    monkeypatch.setattr(telemetry.database, "record_fix_event", record)

    await telemetry.record_fix(object(), kind="cookie_jar", detail="edge:Default → 31 کوکی")

    assert recorded == [("cookie_jar", "edge:Default → 31 کوکی")]


async def test_a_broken_recording_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode(pool: Any, *, kind: str, detail: str = "") -> None:
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(telemetry.database, "record_fix_event", explode)

    await telemetry.record_fix(object(), kind="cookie_jar")

    assert "could not record the cookie_jar fix" in caplog.text


async def test_the_zone_name_handed_to_postgres_is_the_one_we_computed(db: FakeDb) -> None:
    """No tz database and a typo both end as UTC — for the SQL and for the days."""
    await telemetry.build_trend(object(), now=NOW, timezone_name="Mars/Olympus")

    assert db.zone_seen == "UTC"


def test_the_windows_and_guards_are_sane() -> None:
    assert 7 <= telemetry.TREND_DAYS <= 31
    assert 1 / 48 <= telemetry.MIN_EFFECT_DAYS <= 1.0, "hours, not minutes"
