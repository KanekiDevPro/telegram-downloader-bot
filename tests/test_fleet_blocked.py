"""A blocked fleet must fail fast and honestly, not retry a hopeless link.

Measured state: yt-dlp answers every YouTube link with the bot-wall
(``EXTRACTOR_BLOCKED``), the local cobalt node answers
``error.api.youtube.login`` (no session), the public pool node 403s, and the
session generator 503s. Every engine that could serve YouTube is blocked — so
a link must end on attempt 1 with one true sentence, never three attempts with
backoff ending in a generic failure.

Two skip reasons stay apart: a fallback the operator turned off (or never
configured) keeps the original diagnosis; a pool that exists but is entirely
quarantined *is* the fleet condition and converts to the fleet code.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.i18n import error_message
from services import fallback, worker
from services.cobalt import CobaltError
from services.extractor import ExtractionError, classify_block
from services.queue import DownloadTask
from tests.test_fallback import (
    TASK,
    FakeCobalt,
    _install,
    _NeverStopping,
    _Queue,
)


def _session_missing() -> CobaltError:
    """What the embedded node answers for YouTube without a session."""
    return CobaltError(
        "ERROR",
        "instance answered error.api.youtube.login",
        upstream="error.api.youtube.login",
    )


async def test_a_quarantined_pool_converts_a_youtube_block_to_the_fleet_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The skip path's bare ``raise`` is the whole defect: the original error was
    never marked, so nothing short-circuited it and the job burned three attempts."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(
        monkeypatch,
        extract_error=error,
        cobalt=FakeCobalt(available=False, quarantine_reason="ERROR: no session"),
        download_dir=tmp_path,
    )

    with pytest.raises(ExtractionError) as caught:
        await worker.process_download_task(
            TASK, env.bot, env.pool, env.extractor, env.cobalt
        )

    assert caught.value.code == "YOUTUBE_BLOCKED"
    assert caught.value.message == "blocked", "the bot-wall text stays in the log"
    assert isinstance(caught.value.__cause__, ExtractionError)
    assert caught.value.__cause__.code == "EXTRACTOR_BLOCKED"
    assert env.cobalt.resolved == [], "no doomed round trip while quarantined"
    assert not fallback.fallback_was_attempted(caught.value), (
        "this link never reached the fallback — the mark would be a lie"
    )


async def test_a_switched_off_fallback_keeps_the_original_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Case (a): no service, no fleet — the ordinary blocked-link message stands."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(
        monkeypatch,
        extract_error=error,
        cobalt=FakeCobalt(enabled=False),
        download_dir=tmp_path,
    )

    with pytest.raises(ExtractionError) as caught:
        await worker.process_download_task(
            TASK, env.bot, env.pool, env.extractor, env.cobalt
        )

    assert caught.value is error


async def test_a_non_youtube_block_never_becomes_a_youtube_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fleet sentence names YouTube — a Vimeo block must never wear it."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(
        monkeypatch,
        extract_error=error,
        cobalt=FakeCobalt(available=False),
        download_dir=tmp_path,
    )
    vimeo = replace(TASK, url="https://vimeo.com/123456")

    with pytest.raises(ExtractionError) as caught:
        await worker.process_download_task(
            vimeo, env.bot, env.pool, env.extractor, env.cobalt
        )

    assert caught.value is error


async def test_a_sessionless_fallback_converts_the_original_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both engines refused: yt-dlp hit the wall AND cobalt has no session —
    the fleet condition, with both attempts honestly recorded on the error."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(
        monkeypatch,
        extract_error=error,
        cobalt=FakeCobalt(failure=_session_missing()),
        download_dir=tmp_path,
    )

    with pytest.raises(ExtractionError) as caught:
        await worker.process_download_task(
            TASK, env.bot, env.pool, env.extractor, env.cobalt
        )

    assert caught.value.code == "YOUTUBE_BLOCKED"
    assert fallback.fallback_was_attempted(caught.value), "both engines had this link"
    assert isinstance(caught.value.__cause__, CobaltError)


async def test_a_fleet_block_ends_the_job_on_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The user-visible half of the defect: one attempt, no backoff sleep."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(
        monkeypatch,
        download_error=error,
        cobalt=FakeCobalt(available=False),
        download_dir=tmp_path,
    )
    sleeps: list[float] = []

    async def no_sleep(*args: Any, **kwargs: Any) -> bool:
        sleeps.append(1.0)
        return False

    async def record_block(
        pool: Any, task: DownloadTask, err: ExtractionError, jar: Any
    ) -> None:
        return None

    async def no_alert(*args: Any, **kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(worker, "_sleep_until", no_sleep)
    monkeypatch.setattr(worker.telemetry, "record_block", record_block)
    monkeypatch.setattr(worker.telemetry, "maybe_send_early_alert", no_alert)

    await worker._process_with_retry(
        TASK,
        env.bot,  # type: ignore[arg-type]
        env.pool,  # type: ignore[arg-type]
        _Queue(),  # type: ignore[arg-type]
        env.extractor,  # type: ignore[arg-type]
        _NeverStopping(),  # type: ignore[arg-type]
        env.cobalt,  # type: ignore[arg-type]
    )

    assert env.extractor.downloads == 1, "no second attempt, no backoff"
    assert sleeps == [], "the job ends before any retry sleep"


def test_the_fleet_sentence_reads_temporary_in_both_languages() -> None:
    en = error_message("YOUTUBE_BLOCKED", "en")
    fa = error_message("YOUTUBE_BLOCKED", "fa")

    assert en != fa
    for text in (en, fa):
        assert "YOUTUBE_BLOCKED" not in text, "no codes in the chat"
        assert "cobalt" not in text.lower() and "yt-dlp" not in text.lower()
        assert "/" not in text, "no paths in the chat"
    assert "temporarily" in en and "few minutes" in en
    assert "موقت" in fa and "چند دقیقه" in fa
    assert "link is fine" in en and "لینک شما سالم" in fa


def test_a_fleet_block_is_filed_as_a_block_not_a_content_problem() -> None:
    """The digest must keep counting these as blocks, or /blocks goes quiet
    exactly when the fleet is down."""
    error = ExtractionError("YOUTUBE_BLOCKED", "blocked")
    assert classify_block(error, TASK.url, None) == "login"
