"""The search a rewritten Spotify link depends on: retries, and one without the jar.

A Spotify link is served by searching YouTube for the same song, so the *search* is
where that link either works or dies — before the user's download has even started.
Two failures are worth a second attempt there, and both are yt-dlp's own advice:

* a **stale session** ("the page needs to be reloaded") is transient, so it is retried
  with the configured backoff instead of failing a link nobody has tried to download;
* a session the *cookie jar* is the problem in — a rotated visitor binding, or a
  request the site reads as a bot — often succeeds without any cookies at all, so the
  search is repeated once without them.

Bounded, logged, and only for searches: a *download* that fails that way keeps its
existing behaviour, where the block is the answer that drives the user's message, the
admin alert and the jar refresh.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yt_dlp.utils import DownloadError

from services import extractor as extractor_module
from services.extractor import ExtractionError, ExtractorService, SearchHit

HEADER = "# Netscape HTTP Cookie File\n"
HITS = {
    "_type": "playlist",
    "entries": [
        {
            "id": "dQw4w9WgXcQ",
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Official",
            "duration": 213,
        }
    ],
}


def _jar(path: Path) -> Path:
    """A plausible jar — ``using_cookies`` only checks that it holds cookies."""
    path.write_text(
        HEADER + ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\tv\n", encoding="utf-8"
    )
    return path


class _YDL:
    """A stand-in for ``yt_dlp.YoutubeDL`` that can fail a given number of times."""

    instances: list["_YDL"] = []

    #: Per class: how many of the next calls raise, and what they raise.
    failures: list[Exception] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.calls: list[str] = []
        _YDL.instances.append(self)

    def __enter__(self) -> "_YDL":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        self.calls.append(url)
        if _YDL.failures:
            raise _YDL.failures.pop(0)
        return HITS


@pytest.fixture(autouse=True)
def _fake_ydl(monkeypatch: pytest.MonkeyPatch) -> None:
    _YDL.instances = []
    _YDL.failures = []
    monkeypatch.setattr(extractor_module, "yt_dlp", SimpleNamespace(YoutubeDL=_YDL))


def _service(tmp_path: Path, *, cookies: bool = False, retries: int = 0) -> ExtractorService:
    return ExtractorService(
        tmp_path,
        cookie_file=_jar(tmp_path / "cookies.txt") if cookies else None,
        js_runtime="none",
        retry_attempts=retries,
        retry_backoff_s=0.0,
    )


# ---------------------------------------------------------------------------
# Transient failures are retried
# ---------------------------------------------------------------------------


def test_a_stale_session_on_a_search_is_retried_rather_than_failing_the_link(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, retries=1)
    _YDL.failures = [DownloadError("ERROR: The page needs to be reloaded.")]

    hits = service._search_sync("Rick Astley", 5)

    assert hits == [SearchHit("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Official", 213)]
    assert len(_YDL.instances) == 2, "one failure, one retry"


def test_the_retry_budget_is_the_configured_one(tmp_path: Path) -> None:
    service = _service(tmp_path, retries=2)
    _YDL.failures = [
        DownloadError("ERROR: The page needs to be reloaded."),
        DownloadError("ERROR: The page needs to be reloaded."),
    ]

    service._search_sync("Rick Astley", 5)

    assert len(_YDL.instances) == 3


def test_a_search_that_keeps_failing_reports_the_block_it_was(tmp_path: Path) -> None:
    """The code survives, because it is what the user's message and the digest are
    written from — and the attempts stay bounded: two rounds (the configured retry),
    each of them at most a jarred and an anonymous attempt."""
    service = _service(tmp_path, retries=1, cookies=True)
    _YDL.failures = [DownloadError("ERROR: The page needs to be reloaded.")] * 4

    with pytest.raises(ExtractionError) as caught:
        service._search_sync("Rick Astley", 5)

    assert caught.value.code == "SESSION_STALE"
    assert len(_YDL.instances) == 4
    assert ["cookiefile" in ydl.opts for ydl in _YDL.instances] == [True, False, True, False]


def test_a_permanent_failure_is_not_retried(tmp_path: Path) -> None:
    service = _service(tmp_path, retries=2)
    _YDL.failures = [DownloadError("ERROR: Unsupported URL: https://x/1")]

    with pytest.raises(ExtractionError) as caught:
        service._search_sync("anything", 5)

    assert caught.value.code == "UNSUPPORTED_URL"
    assert len(_YDL.instances) == 1, "retrying an unsupported URL helps nobody"


# ---------------------------------------------------------------------------
# The second attempt without the cookie jar
# ---------------------------------------------------------------------------


def test_a_stale_session_with_a_jar_is_repeated_without_it(tmp_path: Path) -> None:
    service = _service(tmp_path, cookies=True)
    _YDL.failures = [DownloadError("ERROR: The page needs to be reloaded.")]

    hits = service._search_sync("Rick Astley", 5)

    assert hits, "the anonymous attempt is what answered"
    first, second = _YDL.instances
    assert "cookiefile" in first.opts, "the configured jar was sent first"
    assert "cookiefile" not in second.opts, "and deliberately not the second time"
    assert "cookiesfrombrowser" not in second.opts


def test_a_bot_check_with_a_jar_is_repeated_without_it_too(tmp_path: Path) -> None:
    """"Sign in to confirm you're not a bot" is the other cookie-caused refusal."""
    service = _service(tmp_path, cookies=True)
    _YDL.failures = [DownloadError("ERROR: Sign in to confirm you're not a bot")]

    assert service._search_sync("Rick Astley", 5)

    assert "cookiefile" not in _YDL.instances[1].opts


def test_no_jar_means_nothing_to_leave_out(tmp_path: Path) -> None:
    service = _service(tmp_path, cookies=False)
    _YDL.failures = [DownloadError("ERROR: Sign in to confirm you're not a bot")]

    with pytest.raises(ExtractionError):
        service._search_sync("Rick Astley", 5)

    assert len(_YDL.instances) == 1, "there was no jar to take a second opinion about"


def test_the_anonymous_attempt_never_hides_a_real_failure(tmp_path: Path) -> None:
    """If both attempts fail, the user gets the *original* diagnosis — the one about
    the link — and not a second, invented one."""
    service = _service(tmp_path, cookies=True)
    _YDL.failures = [
        DownloadError("ERROR: The page needs to be reloaded."),
        DownloadError("ERROR: The page needs to be reloaded."),
    ]

    with pytest.raises(ExtractionError) as caught:
        service._search_sync("Rick Astley", 5)

    assert caught.value.code == "SESSION_STALE"
    assert len(_YDL.instances) == 2, "the jar first, then without it"


def test_a_search_that_worked_once_is_not_searched_twice(tmp_path: Path) -> None:
    service = _service(tmp_path, cookies=True)

    service._search_sync("Rick Astley", 5)

    assert len(_YDL.instances) == 1
    assert "cookiefile" in _YDL.instances[0].opts
