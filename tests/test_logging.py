"""Regression tests for console encoding.

On Windows the default stream encoding is the legacy code page, so logging a
Persian error message (or printing the smoke test's check marks) raised
``UnicodeEncodeError``. ``force_utf8_console`` fixes that.
"""

from __future__ import annotations

import io
import sys

import pytest

from core.logging import _stream_encoding, force_utf8_console

PERSIAN = "\u062f\u0627\u0646\u0644\u0648\u062f"
CHECK_MARK = "\u2714"


def _legacy_stream(monkeypatch: pytest.MonkeyPatch) -> io.TextIOWrapper:
    """A stream that behaves like a cp1252 Windows pipe."""
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    return stream


def test_legacy_console_cannot_encode_persian() -> None:
    # Sanity check that the fixture encoding really is the broken one.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="")
    with pytest.raises(UnicodeEncodeError):
        stream.write(PERSIAN)
    stream.detach()


def test_force_utf8_console_switches_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _legacy_stream(monkeypatch)
    assert _stream_encoding(stream) == "cp1252"

    force_utf8_console()

    assert _stream_encoding(stream).lower().replace("-", "") == "utf8"
    stream.write(PERSIAN + CHECK_MARK)  # must not raise anymore
    stream.flush()


def test_force_utf8_console_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _legacy_stream(monkeypatch)
    force_utf8_console()
    force_utf8_console()
    assert _stream_encoding(stream).lower().replace("-", "") == "utf8"


def test_force_utf8_console_tolerates_streams_without_reconfigure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # StringIO has no reconfigure(); the helper must leave it alone and not raise.
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    force_utf8_console()
