"""Unit tests for core.utils — hashing, URL handling, formatting, escaping."""

from __future__ import annotations

from datetime import date

import pytest

from core.utils import (
    canonical_url,
    escape_html,
    extract_url,
    format_size,
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
