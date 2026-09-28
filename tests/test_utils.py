"""Unit tests for core.utils — hashing, URL handling, formatting, escaping."""

from __future__ import annotations

import datetime as _dt
import importlib.util
from datetime import date, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.utils import (
    canonical_url,
    escape_html,
    extract_url,
    format_size,
    local_midnight,
    sanitize_filename,
    sha256_hex,
    today_local,
    validate_url,
)


def test_sha256_hex_is_stable_and_64_chars() -> None:
    digest = sha256_hex("hello")
    assert digest == sha256_hex("hello")
    assert len(digest) == 64
    assert sha256_hex("hello") != sha256_hex("hello!")


def test_canonical_url_strips_tracking_params_and_fragment() -> None:
    assert (
        canonical_url("https://youtu.be/x?utm_source=a&fbclid=b&t=10#frag")
        == "https://youtu.be/x?t=10"
    )


def test_canonical_url_keeps_meaningful_params() -> None:
    assert canonical_url("https://www.youtube.com/watch?v=abc123") == (
        "https://www.youtube.com/watch?v=abc123"
    )


def test_canonical_url_without_query_is_unchanged() -> None:
    assert canonical_url("https://youtu.be/abc") == "https://youtu.be/abc"


def test_extract_url_finds_first_url() -> None:
    assert extract_url("look at https://youtu.be/abc now") == "https://youtu.be/abc"


@pytest.mark.parametrize("text", ["", "no link here", "www.youtube.com/watch?v=x"])
def test_extract_url_returns_none_when_absent(text: str) -> None:
    assert extract_url(text) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://youtu.be/abc", True),
        ("http://example.com/v/1", True),
        ("ftp://example.com/file", False),
        ("youtu.be/abc", False),
        ("https://", False),
    ],
)
def test_validate_url(url: str, expected: bool) -> None:
    assert validate_url(url) is expected


def test_format_size_units() -> None:
    assert format_size(1536) == "1.5 KB"
    assert format_size(1024 * 1024) == "1.0 MB"
    assert format_size(1024**3) == "1.0 GB"


def test_format_size_for_missing_values() -> None:
    # Both "unknown" inputs must render identically (no crash, no "0 B").
    assert format_size(0) == format_size(None)


def test_format_size_borrows_the_callers_word_for_unknown() -> None:
    """The word goes where the number would, in whoever is reading's language."""
    assert format_size(None, unknown="نامشخص") == "نامشخص"
    assert format_size(0, unknown="unknown") == "unknown"
    assert format_size(None) == "?", "the default stays neutral for scripts"


def test_sanitize_filename_replaces_illegal_characters() -> None:
    assert sanitize_filename('a/b:c*d?e"f<g>h|i') == "a_b_c_d_e_f_g_h_i"


def test_sanitize_filename_never_returns_empty() -> None:
    assert sanitize_filename("...") == "media"
    assert sanitize_filename("") == "media"


def test_sanitize_filename_truncates() -> None:
    assert len(sanitize_filename("x" * 500)) == 120


def test_sanitize_filename_keeps_extensions() -> None:
    assert sanitize_filename("Clip [dQw4w9WgXcQ].mp4") == "Clip [dQw4w9WgXcQ].mp4"


def test_escape_html() -> None:
    assert escape_html("<b>Tom & Jerry</b>") == "&lt;b&gt;Tom &amp; Jerry&lt;/b&gt;"


def test_today_local_returns_a_date() -> None:
    assert isinstance(today_local(), date)


@pytest.mark.parametrize("raw", ["", "   ", "Not/AZone"])
def test_a_broken_timezone_setting_degrades_to_utc_never_to_a_crash(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """The quota path reads the timezone on every message: a misconfigured
    TIMEZONE must cost a UTC day boundary, not a 500 per message. An empty
    key raises ValueError inside ZoneInfo, an unknown name raises
    ZoneInfoNotFoundError; both fall back the same way.
    """
    monkeypatch.setattr("core.utils.get_settings", lambda: SimpleNamespace(timezone=raw))
    assert isinstance(today_local(), date)
    assert local_midnight(date(2026, 9, 28)).utcoffset() == timezone.utc.utcoffset(None)


def test_tzdata_ships_so_zoneinfo_resolves_on_every_platform() -> None:
    """``ZoneInfo`` needs a tz database and Windows has none: without the
    ``tzdata`` package the configured TIMEZONE cannot resolve, every lookup
    silently falls back to UTC (see :func:`today_local`) and the daily quota
    boundary moves from local midnight to 03:30. The package is a
    *dependency*, not a platform accident — a Linux container ships the
    system database and hides the gap entirely, which is why it went
    unnoticed. Pinned by importability: this is what the quota boundary
    rests on."""
    assert importlib.util.find_spec("tzdata") is not None, (
        "tzdata must be importable on every platform — it is a declared "
        "dependency, and today_local() silently degrades to UTC without it"
    )


def test_the_daily_boundary_follows_the_configured_timezone_not_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The quota boundary is the *configured* zone's midnight, pinned on a
    clock deliberately frozen where UTC and that zone disagree about the
    date.

    21:30 UTC on January 1st is already 01:00 on January 2nd in Tehran
    (UTC+3:30): a boundary computed in UTC calls that still "yesterday",
    so a user's daily quota resets at 03:30 local instead of midnight.
    The clock is seeded at exactly that instant and the expectation is the
    zone's own date — never ``date.today()``, which is a different
    timezone's opinion of the day and the reason this bug hid behind a
    "midnight flake" diagnosis."""
    frozen = _dt.datetime(2026, 1, 1, 21, 30, tzinfo=_dt.timezone.utc)

    class _FrozenClock(_dt.datetime):
        @classmethod
        def now(cls, tz: _dt.tzinfo | None = None) -> "_FrozenClock":
            return cls.fromtimestamp(frozen.timestamp(), tz)

    monkeypatch.setattr("core.utils.datetime", _FrozenClock)
    monkeypatch.setattr(
        "core.utils.get_settings", lambda: SimpleNamespace(timezone="Asia/Tehran")
    )

    tehran_day = frozen.astimezone(_dt.timezone(_dt.timedelta(hours=3, minutes=30))).date()
    assert tehran_day == date(2026, 1, 2), "the seed is really a different day"
    assert today_local() == tehran_day, (
        "the boundary is Tehran's midnight — the configured zone's own date"
    )
    assert today_local() != frozen.date(), "never the UTC day"
    assert today_local() == frozen.astimezone(ZoneInfo("Asia/Tehran")).date(), (
        "and the configured zone's own calendar agrees — the expectation is "
        "datetime.now(ZoneInfo(settings.timezone)), never date.today()"
    )
