"""Production stage telemetry (Track B): timing lifecycle and safe metrics.

The helpers are pure and defensive: missing/invalid timings become ``None``,
never invented speeds; URL identity is a hash, never the URL; emitting never
raises, so telemetry can never fail a download.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from services import telemetry as telemetry_module


def _timings() -> Any:
    timings = telemetry_module.new_timings(received_at=1000.0)
    timings.started_at = 1000.0
    return timings


def test_timing_lifecycle_produces_stage_milliseconds() -> None:
    t = _timings()
    t.probe_started_at = 1001.0
    t.probe_finished_at = 1003.5
    t.download_started_at = 1004.0
    t.download_finished_at = 1014.0
    t.processing_started_at = 1014.0
    t.processing_finished_at = 1015.0
    t.upload_started_at = 1015.0
    t.upload_finished_at = 1020.0
    t.completed_at = 1020.0

    record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc123", timings=t, ok=True
    )

    assert record["probe_ms"] == 2500.0
    assert record["download_ms"] == 10000.0
    assert record["processing_ms"] == 1000.0
    assert record["upload_ms"] == 5000.0
    assert record["total_ms"] == 20000.0
    assert record["platform"] == "youtube"
    assert record["ok"] is True


def test_missing_timing_information_becomes_none() -> None:
    record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc123", timings=_timings(), ok=True
    )

    assert record["probe_ms"] is None
    assert record["download_ms"] is None
    assert record["processing_ms"] is None
    assert record["upload_ms"] is None
    assert record["total_ms"] is None
    assert record["download_mbps"] is None
    assert record["upload_mbps"] is None


def test_zero_and_invalid_durations_are_handled() -> None:
    assert telemetry_module.stage_ms(5.0, 5.0) == 0.0
    assert telemetry_module.stage_ms(None, 5.0) is None
    assert telemetry_module.stage_ms(5.0, None) is None
    assert telemetry_module.stage_ms(6.0, 5.0) is None
    assert telemetry_module.stage_ms("x", 5.0) is None
    assert telemetry_module.safe_mbps(8_000_000, 0.0) is None
    assert telemetry_module.safe_mbps(None, 4.0) is None
    assert telemetry_module.safe_mbps(-10, 4.0) is None


def test_byte_counters_and_mbps_calculation() -> None:
    t = _timings()
    t.download_started_at = 1000.0
    t.download_finished_at = 1008.0
    t.download_bytes = 8_000_000
    t.upload_started_at = 1008.0
    t.upload_finished_at = 1010.0
    t.upload_bytes = 8_000_000

    record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc123", timings=t, ok=True
    )

    assert record["download_bytes"] == 8_000_000
    assert record["upload_bytes"] == 8_000_000
    assert record["download_mbps"] == 8.0
    assert record["upload_mbps"] == 32.0


def test_no_secret_leakage_in_record_or_digest() -> None:
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&sig=SECRET123"
    digest = telemetry_module.url_digest(url)

    assert digest != ""
    assert "SECRET123" not in digest
    assert "watch" not in digest
    assert telemetry_module.url_digest(url) == digest  # stable

    record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash=digest, timings=_timings(), ok=True
    )
    blob = json.dumps(record)
    assert "SECRET123" not in blob
    assert "watch?v=" not in blob
    assert "bot_token" not in blob.lower()
    assert "cookie" not in blob.lower()


def test_telemetry_failure_cannot_fail_a_download(caplog: Any) -> None:
    class Exploding:
        def __float__(self) -> float:
            raise RuntimeError("boom")

    t = _timings()
    t.download_started_at = cast_any(Exploding())

    with caplog.at_level(logging.INFO, logger="services.telemetry"):
        record = telemetry_module.download_metrics_record(
            platform="youtube", url_hash="abc", timings=t, ok=True
        )
        telemetry_module.emit_download_metrics(record)
        telemetry_module.emit_download_metrics({"unserializable": object()})

    assert record["download_ms"] is None


def cast_any(value: Any) -> Any:
    return value


def test_retry_attempts_keep_coherent_totals() -> None:
    first = _timings()
    first.download_started_at = 1001.0
    first.download_finished_at = 1005.0
    first.completed_at = 1005.0
    second = _timings()
    second.download_started_at = 1006.0
    second.download_finished_at = 1012.0
    second.completed_at = 1012.0

    first_record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc", timings=first, ok=False
    )
    second_record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc", timings=second, ok=True
    )

    assert first_record["total_ms"] == 5000.0
    assert second_record["total_ms"] == 12000.0
    assert second_record["total_ms"] >= (first_record["total_ms"] or 0.0)


def test_upload_timing_is_separate_from_download_timing() -> None:
    t = _timings()
    t.download_started_at = 1000.0
    t.download_finished_at = 1010.0
    t.upload_started_at = 1050.0
    t.upload_finished_at = 1060.0

    record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc", timings=t, ok=True
    )

    assert record["download_ms"] == 10000.0
    assert record["upload_ms"] == 10000.0
