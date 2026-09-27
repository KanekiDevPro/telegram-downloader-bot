"""Retry policy tests (offline, no sleeping).

YouTube's stale-session error ("The page needs to be reloaded") deserves
another attempt — but never a *slept* one: it is a client/session mismatch
that no 3 s/6 s backoff ever healed (production burned ~9 s a job proving it),
so the next attempt is a failover *sideways* — the next fallback client, jar
left behind — run immediately. Only that class of failure gets even that, and
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


#: The shipped default list — the request shape production actually sends.
DEFAULT_CLIENTS = ("android", "ios", "mweb", "tv", "web")

#: A jar with one real cookie row — ``using_cookies`` insists on at least one.
VALID_JAR = (
    "# Netscape HTTP Cookie File\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t2147483647\tLOGIN_INFO\tv\n"
)


def _service(
    tmp_path: Path,
    *,
    attempts: int = 2,
    backoff: float = 3.0,
    cookie_file: Path | None = None,
) -> ExtractorService:
    return ExtractorService(
        tmp_path,
        js_runtime="none",
        cookie_file=cookie_file,
        youtube_clients=DEFAULT_CLIENTS,
        retry_attempts=attempts,
        retry_backoff_s=backoff,
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


def _flaky(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[object],
    steps: list[tuple[tuple[str, ...] | None, bool]] | None = None,
) -> list[int]:
    """Make each metadata attempt return/raise the next scripted outcome.

    ``steps`` records the ``(clients, allow_cookies)`` each attempt ran with,
    so the failover order itself is pin-able.
    """
    calls = [0]

    def fake_attempt(
        _self: ExtractorService,
        url: str,
        *,
        youtube_clients: tuple[str, ...] | None = None,
        allow_cookies: bool = True,
    ) -> MediaInfo:
        outcome = outcomes[min(calls[0], len(outcomes) - 1)]
        calls[0] += 1
        if steps is not None:
            steps.append((youtube_clients, allow_cookies))
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


def test_a_stale_session_fails_over_to_the_next_fallback_client_without_sleeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"The page needs to be reloaded" is a client/session mismatch — the old
    budget answered it with 3 s and 6 s sleeps that never healed one. The next
    attempt is a failover *sideways* instead, run immediately: the next
    fallback client of the metadata priority, jar left behind."""
    sleeps = _capture_sleeps(monkeypatch)
    steps: list[tuple[tuple[str, ...] | None, bool]] = []
    calls = _flaky(monkeypatch, [STALE, _info()], steps=steps)

    result = _service(tmp_path)._extract_sync("https://youtu.be/x")

    assert result.title == "Big Buck Bunny"
    assert calls[0] == 2
    assert sleeps == [], "a stale session never heals by sleeping"
    assert steps == [(None, True), (("web_embedded",), False)], (
        "the next fallback client, jar left behind"
    )


def test_a_stale_session_with_a_jar_fails_over_jarless_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed-in request's first sidestep leaves the jar behind — yt-dlp's
    own "retry without the cookies", the second opinion ``_search_attempt``
    already takes — and only then does the walk reach the next client."""
    sleeps = _capture_sleeps(monkeypatch)
    steps: list[tuple[tuple[str, ...] | None, bool]] = []
    calls = _flaky(monkeypatch, [STALE, STALE, _info()], steps=steps)
    jar = tmp_path / "cookies.txt"
    jar.write_text(VALID_JAR, encoding="utf-8")

    result = _service(tmp_path, cookie_file=jar)._extract_sync("https://youtu.be/x")

    assert result.title == "Big Buck Bunny"
    assert calls[0] == 3
    assert sleeps == []
    assert steps == [
        (None, True),
        (None, False),
        (("web_embedded",), False),
    ]


def test_the_failover_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that answers every client "reloaded" gets the chain walked once —
    each request tried exactly once, nothing slept — and then its failure."""
    sleeps = _capture_sleeps(monkeypatch)
    steps: list[tuple[tuple[str, ...] | None, bool]] = []
    calls = _flaky(monkeypatch, [STALE], steps=steps)

    with pytest.raises(ExtractionError) as caught:
        _service(tmp_path, attempts=2)._extract_sync("https://youtu.be/x")

    assert caught.value.code == "SESSION_STALE"
    assert calls[0] == 2  # the request + one fallback client — no repeats
    assert len(set(steps)) == len(steps), "no request is tried twice"
    assert sleeps == []


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


def test_every_failover_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _capture_sleeps(monkeypatch)
    _flaky(monkeypatch, [STALE, _info()])

    with caplog.at_level(logging.INFO):
        _service(tmp_path)._extract_sync("https://youtu.be/x")

    messages = [record.getMessage() for record in caplog.records]
    assert any("failing over" in message and "(attempt 2 of 2)" in message for message in messages)


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
