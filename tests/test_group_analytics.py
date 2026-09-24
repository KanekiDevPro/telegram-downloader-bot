"""Group analytics: what the panel knows about groups, and who may read it.

What is recorded is where, when and whether a download worked — a chat id, its
name when one is known, and an error *code*. Never content, never user data.
The aggregation is the database's job (SQL ``GROUP BY``); Python only renders
what came back. And the screen is admin-only, re-checked server-side on every
single tap.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core import database
from core.i18n import DEFAULT_LANG, t
from handlers import admin
from services import panel, worker
from services.queue import DownloadTask

EN = "en"
GROUP = -1009876543210


class _Pool:
    def __init__(self) -> None:
        self.rows: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.rows.append((query, args))
        return "INSERT 0 1"

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.rows.append((query, args))
        return []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        self.rows.append((query, args))
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.rows.append((query, args))
        return 0


def _task(chat_id: int, **over: Any) -> DownloadTask:
    fields: dict[str, Any] = {
        "chat_id": chat_id,
        "telegram_id": 5,
        "url": "https://youtu.be/abc",
        "media_format": "video",
        "lang": "fa",
        "title": "A Clip",
        "chat_title": "Group Chat",
    }
    fields.update(over)
    return DownloadTask(**fields)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


async def test_a_group_outcome_is_recorded() -> None:
    pool = _Pool()
    await worker._note_group_download(pool, _task(GROUP), ok=True)
    queries = [query for query, _args in pool.rows]
    assert any("group_downloads" in query for query in queries)
    assert pool.rows[0][1] == (GROUP, "Group Chat", True, "")


async def test_a_failure_records_its_code_not_its_story() -> None:
    pool = _Pool()
    await worker._note_group_download(pool, _task(GROUP), ok=False, code="http_403")
    assert pool.rows[0][1] == (GROUP, "Group Chat", False, "http_403")


async def test_private_downloads_are_nobody_s_group_analytics() -> None:
    pool = _Pool()
    await worker._note_group_download(pool, _task(5), ok=True)
    assert pool.rows == []


# ---------------------------------------------------------------------------
# Aggregation stays in SQL
# ---------------------------------------------------------------------------


async def test_top_groups_aggregates_in_the_database() -> None:
    pool = _Pool()
    await database.top_groups(pool, limit=5)
    query, args = pool.rows[0]
    assert "GROUP BY chat_id" in query
    assert "LIMIT $1" in query
    assert args == (5,)
    assert "chat_title" in query, "the name is read — when one is known"
    assert "message" not in query.lower(), "never any message content"


async def test_the_summary_is_one_aggregate_row() -> None:
    pool = _Pool()
    await database.group_usage_summary(pool)
    query, _args = pool.rows[0]
    assert "count(DISTINCT chat_id)" in query
    assert "FILTER (WHERE ok)" in query


async def test_failure_codes_are_counts_and_nothing_else() -> None:
    pool = _Pool()
    await database.group_failure_codes(pool)
    query, _args = pool.rows[0]
    assert "GROUP BY code" in query
    assert "count(*)" in query


async def test_the_week_stats_are_one_aggregate_row_with_two_windows() -> None:
    pool = _Pool()
    prev_start = datetime(2026, 9, 7, tzinfo=timezone.utc)
    cur_start = datetime(2026, 9, 14, tzinfo=timezone.utc)
    cur_end = datetime(2026, 9, 21, tzinfo=timezone.utc)
    await database.group_week_stats(
        pool, prev_start=prev_start, cur_start=cur_start, cur_end=cur_end
    )
    query, args = pool.rows[0]
    assert "FILTER (WHERE created_at >= $2 AND created_at < $3)" in query
    assert "FILTER (WHERE created_at >= $1 AND created_at < $2)" in query
    assert "FILTER (WHERE NOT ok" in query
    assert args == (prev_start, cur_start, cur_end)
    assert "GROUP BY" not in query, "one row, aggregated in SQL — never in Python"


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _patch_screen(
    monkeypatch: pytest.MonkeyPatch,
    *,
    summary: dict[str, Any],
    top: list[dict[str, Any]],
    codes: list[dict[str, Any]],
    week: dict[str, Any] | None = None,
) -> None:
    async def fake_summary(pool: Any) -> Any:
        return summary

    async def fake_top(pool: Any, *, limit: int = 5) -> Any:
        return top

    async def fake_codes(pool: Any, *, limit: int = 5) -> Any:
        return codes

    async def fake_week(pool: Any, **_windows: Any) -> Any:
        return week or {"cur_total": 0, "cur_failed": 0, "prev_total": 0, "prev_failed": 0}

    monkeypatch.setattr(panel.database, "group_usage_summary", fake_summary)
    monkeypatch.setattr(panel.database, "top_groups", fake_top)
    monkeypatch.setattr(panel.database, "group_failure_codes", fake_codes)
    monkeypatch.setattr(panel.database, "group_week_stats", fake_week)


async def test_the_groups_screen_reports_only_what_the_rows_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_screen(
        monkeypatch,
        summary={"total": 1284, "successes": 1231, "failed": 53, "groups": 37},
        top=[
            {"chat_id": GROUP, "chat_title": "Group A", "total": 324, "failed": 12},
            {"chat_id": -1002, "chat_title": "", "total": 164, "failed": 3},
        ],
        codes=[{"code": "http_403", "count": 9}],
    )
    text = await panel.groups_text(object(), EN)
    assert "1,284" in text and "1,231" in text and "37" in text
    assert "Group A" in text and "324" in text
    assert "-1002" in text, "an unnamed group is its id — never an invented name"
    assert "http_403" in text


async def test_an_empty_groups_screen_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_screen(
        monkeypatch,
        summary={"total": 0, "successes": 0, "failed": 0, "groups": 0},
        top=[],
        codes=[],
    )
    text = await panel.groups_text(object(), EN)
    assert t("admin.groups_empty", EN) in text
    assert t("admin.groups_failures", EN, failed="0") not in text


# ---------------------------------------------------------------------------
# Week-over-week
# ---------------------------------------------------------------------------


def _week(monkeypatch: pytest.MonkeyPatch, **stats: int) -> Any:
    async def fake_week(pool: Any, **_windows: Any) -> Any:
        return {
            "cur_total": stats.get("cur_total", 0),
            "cur_failed": stats.get("cur_failed", 0),
            "prev_total": stats.get("prev_total", 0),
            "prev_failed": stats.get("prev_failed", 0),
        }

    monkeypatch.setattr(panel.database, "group_week_stats", fake_week)
    return fake_week


def test_the_movement_helpers_sign_their_answers() -> None:
    assert panel._signed_count(1184, 1000) == "+184"
    assert panel._signed_count(988, 1000) == "-12"
    assert panel._signed_percent(1184, 1000) == "+18.4%"
    assert panel._signed_percent(988, 1000) == "-1.2%"
    assert panel._signed_points(2.7, 4.0) == "-1.3%"


async def test_a_growing_week_shows_both_movement_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _week(monkeypatch, cur_total=1184, cur_failed=32, prev_total=1000, prev_failed=40)
    lines = await panel._week_block(object(), EN)
    text = "\n".join(lines)
    assert t("admin.groups_week_title", EN) in text
    assert "1,184" in text
    assert "+184" in text and "+18.4%" in text
    assert "2.7%" in text, "the failure rate of the week itself"
    assert "-1.3%" in text, "failure rate moving in the right direction"


async def test_a_shrinking_week_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _week(monkeypatch, cur_total=988, cur_failed=50, prev_total=1000, prev_failed=40)
    text = "\n".join(await panel._week_block(object(), EN))
    assert "-12" in text and "-1.2%" in text
    assert "+1.1%" in text, "worse failure rate, named as movement"


async def test_a_first_week_has_no_baseline_and_does_not_invent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _week(monkeypatch, cur_total=5, cur_failed=1, prev_total=0, prev_failed=0)
    text = "\n".join(await panel._week_block(object(), EN))
    assert t("admin.groups_week_volume_first", EN) in text
    assert t("admin.groups_week_fail_first", EN) in text
    assert "20.0%" in text, "this week's own rate is still honest"
    assert "+18.4%" not in text and "%!" not in text


async def test_a_silent_current_week_has_no_rate_to_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _week(monkeypatch, cur_total=0, cur_failed=0, prev_total=12, prev_failed=1)
    text = "\n".join(await panel._week_block(object(), EN))
    assert t("admin.groups_week_fail_rate", EN, rate="—") in text
    assert "-12" in text and "-100.0%" in text
    assert t("admin.groups_week_fail_delta", EN, delta="—") in text


async def test_two_silent_weeks_are_not_a_trend(monkeypatch: pytest.MonkeyPatch) -> None:
    _week(monkeypatch, cur_total=0, cur_failed=0, prev_total=0, prev_failed=0)
    assert await panel._week_block(object(), EN) == []


async def test_the_trend_sits_on_the_groups_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_screen(
        monkeypatch,
        summary={"total": 10, "successes": 9, "failed": 1, "groups": 2},
        top=[],
        codes=[],
        week={"cur_total": 1184, "cur_failed": 32, "prev_total": 1000, "prev_failed": 40},
    )
    text = await panel.groups_text(object(), EN)
    assert t("admin.groups_week_title", EN) in text
    assert "+18.4%" in text


async def test_an_unreadable_week_never_breaks_the_groups_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_week(pool: Any, **_windows: Any) -> Any:
        raise RuntimeError("database down")

    _patch_screen(
        monkeypatch,
        summary={"total": 10, "successes": 9, "failed": 1, "groups": 2},
        top=[],
        codes=[],
    )
    monkeypatch.setattr(panel.database, "group_week_stats", broken_week)
    text = await panel.groups_text(object(), EN)
    assert t("admin.groups_headline", EN) in text, "the screen survives its trend"
    assert t("admin.groups_week_title", EN) not in text


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def test_a_crafted_groups_callback_is_rejected_for_non_admins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forwarded panel is still guarded: the check is per-tap, server-side."""
    monkeypatch.setattr(
        admin,
        "get_settings",
        lambda: SimpleNamespace(is_admin=lambda uid: uid == 1),
    )
    cb = SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="admin:groups",
        message=None,
        answer=AsyncMock(),
    )
    await admin.on_panel_button(cb, object(), object())
    cb.answer.assert_awaited_once()
    assert cb.answer.await_args.args[0] == t("admin.only", DEFAULT_LANG)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


def test_the_groups_section_is_wired_into_the_panel() -> None:
    """Groups lives under its category (Users and groups), one tap deep."""
    assert "groups" in admin._PANEL_SCREENS
    markup = admin._category_keyboard(EN, "cat_users")
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(button.callback_data == "admin:groups" for button in buttons)
    assert any(button.text == t("admin.btn_groups", EN) for button in buttons)
