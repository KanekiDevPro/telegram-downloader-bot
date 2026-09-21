"""Retry policy tests (offline, no sleeping).

YouTube's stale-session error ("The page needs to be reloaded") is transient
often enough to deserve another attempt — but only that class of failure, and
only a bounded number of times, so a genuinely blocked host still fails fast
instead of burning the user's queue slot. These tests pin both halves.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

from services import extractor as extractor_module
from services.extractor import (
    RETRYABLE_EXTRACTION_CODES,
    ExtractionError,
    ExtractorService,
    MediaInfo,
)

STALE = ExtractionError("SESSION_STALE", "سشن کهنه است")
BLOCKED = ExtractionError("EXTRACTOR_BLOCKED", "سایت مبدأ مسدود کرد")


def _service(tmp_path: Path, *, attempts: int = 2, backoff: float = 3.0) -> ExtractorService:
    return ExtractorService(
        tmp_path, js_runtime="none", retry_attempts=attempts, retry_backoff_s=backoff
    )


def _capture_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff instead of waiting for it."""
    slept: list[float] = []
    monkeypatch.setattr(extractor_module.time, "sleep", slept.append)
    return slept


def _info() -> MediaInfo:
    return MediaInfo(
        source_url="https://youtu.be/x",
        title="Big Buck Bunny",
        platform="youtube",
        webpage_url="https://youtu.be/x",
        extension="mp4",
        thumbnail=None,
        duration=596,
        filesize_approx=None,
        is_live=False,
    )


def _flaky(monkeypatch: pytest.MonkeyPatch, outcomes: list[object]) -> list[int]:
    """Make each metadata attempt return/raise the next scripted outcome."""
    calls = [0]

    def fake_attempt(_self: ExtractorService, url: str) -> MediaInfo:
        outcome = outcomes[min(calls[0], len(outcomes) - 1)]
        calls[0] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    monkeypatch.setattr(ExtractorService, "_extract_attempt", fake_attempt)
    return calls


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def test_only_the_stale_session_error_is_retryable() -> None:
    assert RETRYABLE_EXTRACTION_CODES == frozenset({"SESSION_STALE"})


def test_stale_session_is_retried_until_it_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps = _capture_sleeps(monkeypatch)
    calls = _flaky(monkeypatch, [STALE, STALE, _info()])

    result = _service(tmp_path)._extract_sync("https://youtu.be/x")

    assert result.title == "Big Buck Bunny"
    assert calls[0] == 3
    assert sleeps == [3.0, 6.0]  # exponential, base = EXTRACTOR_RETRY_BACKOFF_S


def test_retries_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _capture_sleeps(monkeypatch)
    calls = _flaky(monkeypatch, [STALE])

    with pytest.raises(ExtractionError) as caught:
        _service(tmp_path, attempts=2)._extract_sync("https://youtu.be/x")

    assert caught.value.code == "SESSION_STALE"
    assert calls[0] == 3  # first try + 2 retries
    assert sleeps == [3.0, 6.0]


def test_zero_attempts_means_a_single_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _capture_sleeps(monkeypatch)
    calls = _flaky(monkeypatch, [STALE])

    with pytest.raises(ExtractionError):
        _service(tmp_path, attempts=0)._extract_sync("https://youtu.be/x")

    assert calls[0] == 1
    assert sleeps == []


def test_blocks_are_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A flagged IP fails the same way every time; retrying only wastes the slot."""
    sleeps = _capture_sleeps(monkeypatch)
    calls = _flaky(monkeypatch, [BLOCKED])

    with pytest.raises(ExtractionError) as caught:
        _service(tmp_path)._extract_sync("https://youtu.be/x")

    assert caught.value.code == "EXTRACTOR_BLOCKED"
    assert calls[0] == 1
    assert sleeps == []


def test_every_retry_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _capture_sleeps(monkeypatch)
    _flaky(monkeypatch, [STALE, _info()])

    with caplog.at_level(logging.INFO):
        _service(tmp_path)._extract_sync("https://youtu.be/x")

    messages = [record.getMessage() for record in caplog.records]
    assert any("retrying in 3.0s (attempt 2 of 3)" in message for message in messages)


# ---------------------------------------------------------------------------
# Downloads: a retry must not leave the failed attempt's files behind
# ---------------------------------------------------------------------------

async def test_download_retry_cleans_the_failed_attempt_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capture_sleeps(monkeypatch)
    calls = [0]

    class FakeYDL:
        def __init__(self, opts: dict[str, object]) -> None:
            self.opts = opts

        def __enter__(self) -> FakeYDL:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = False) -> dict[str, object]:
            calls[0] += 1
            if calls[0] == 1:
                raise DownloadError("ERROR: [youtube] x: The page needs to be reloaded.")
            target = Path(str(self.opts["outtmpl"])).parent
            (target / "Big Buck Bunny.mp4").write_bytes(b"data")
            return {"title": "Big Buck Bunny", "extractor_key": "Youtube", "ext": "mp4"}

    monkeypatch.setattr(extractor_module.yt_dlp, "YoutubeDL", FakeYDL)

    result = await _service(tmp_path).download("https://youtu.be/x", "video")

    assert result.file_path.name == "Big Buck Bunny.mp4"
    assert calls[0] == 2
    # Only the successful attempt keeps a directory.
    assert len(list(tmp_path.glob("job-*"))) == 1
