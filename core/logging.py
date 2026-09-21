"""Logging setup for the whole process."""

from __future__ import annotations

import logging
import sys
from typing import Any


def force_utf8_console() -> None:
    """Reconfigure stdout/stderr to UTF-8 when they are not already.

    On Windows, Python defaults to the legacy code page (cp1252/cp437) for pipes
    and some consoles. Logging a Persian error message — or printing the ✔/✘
    checks in ``scripts/smoke.py`` — then raises ``UnicodeEncodeError``. A
    no-op on POSIX and wherever UTF-8 is already active; ``errors="replace"``
    keeps output flowing even into a terminal that can't render the glyphs.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # e.g. a captured/redirected stream we don't own
            continue
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if encoding in {"utf8", "utf8mb4"}:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - detached/closed stream
            pass


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging with a single console handler."""
    force_utf8_console()
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # Keep the noisiest libraries quiet unless they really matter.
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _stream_encoding(stream: Any) -> str:
    """Exposed for the test suite: the encoding a stream would currently use."""
    return str(getattr(stream, "encoding", "") or "")
