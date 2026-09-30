"""Queue-wait telemetry (P0-1): enqueue-to-worker-start latency, honestly measured.

With a small worker pool the dominant delay is the time a job spends *waiting*
for a worker, not any download stage. ``queue_wait_ms`` names exactly that:
worker-start minus the gateway's monotonic enqueue stamp — and ``None`` whenever
the wait is unmeasurable (no stamp, a stamp from another process lifetime, or a
backwards clock), never a guessed or negative number.
"""

from __future__ import annotations

import json
from typing import Any

from services import telemetry as telemetry_module
from services.queue import BOOT_ID, DownloadTask


def _timings(enqueued_mono: Any, enqueued_by: Any, started_at: Any) -> Any:
    timings = telemetry_module.new_timings(received_at=1000.0)
    timings.enqueued_mono = enqueued_mono
    timings.enqueued_by = enqueued_by
    timings.started_at = started_at
    return timings


def _record(timings: Any) -> dict[str, object]:
    return telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc123", timings=timings, ok=True
    )


def test_measured_queue_wait_is_reported_in_ms() -> None:
    record = _record(_timings(1000.0, BOOT_ID, 1007.5))

    assert record["queue_wait_ms"] == 7500.0


def test_zero_queue_wait_is_honestly_zero() -> None:
    record = _record(_timings(1000.0, BOOT_ID, 1000.0))

    assert record["queue_wait_ms"] == 0.0


def test_missing_enqueue_stamp_reads_as_none() -> None:
    assert _record(_timings(None, BOOT_ID, 1007.5))["queue_wait_ms"] is None
    assert _record(_timings(0.0, BOOT_ID, 1007.5))["queue_wait_ms"] is None


def test_stamp_from_another_process_lifetime_reads_as_none() -> None:
    # A monotonic stamp is only valid inside the process that made it: a task
    # that crossed a restart (or a second process) must not produce a number.
    assert _record(_timings(1000.0, "another-process-token", 1007.5))["queue_wait_ms"] is None
    assert _record(_timings(1000.0, "", 1007.5))["queue_wait_ms"] is None


def test_backwards_or_hostile_stamps_read_as_none() -> None:
    assert _record(_timings(1007.5, BOOT_ID, 1000.0))["queue_wait_ms"] is None
    assert _record(_timings("x", BOOT_ID, 1007.5))["queue_wait_ms"] is None
    assert _record(_timings(True, BOOT_ID, 1007.5))["queue_wait_ms"] is None
    assert _record(_timings(1000.0, BOOT_ID, None))["queue_wait_ms"] is None


def test_old_payload_without_the_new_fields_still_deserializes() -> None:
    # The Redis queue may hold payloads written before this field existed during
    # a deploy — they must keep working and read the wait as unknown.
    raw = json.dumps(
        {
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "telegram_id": 1,
            "chat_id": 1,
            "queued_at": 1000.0,
        }
    )
    task = DownloadTask.from_payload(raw)

    assert task.enqueued_mono == 0.0
    assert task.enqueued_by == ""

    timings = telemetry_module.new_timings(task.queued_at)
    timings.enqueued_mono = task.enqueued_mono
    timings.enqueued_by = task.enqueued_by
    timings.started_at = 1007.5
    assert _record(timings)["queue_wait_ms"] is None


def test_new_fields_survive_a_payload_round_trip() -> None:
    task = DownloadTask(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        telegram_id=1,
        chat_id=1,
        enqueued_mono=1234.5,
        enqueued_by=BOOT_ID,
    )
    revived = DownloadTask.from_payload(task.to_payload())

    assert revived.enqueued_mono == 1234.5
    assert revived.enqueued_by == BOOT_ID


def test_existing_stage_keys_are_unchanged_and_queue_wait_is_added() -> None:
    record = _record(_timings(1000.0, BOOT_ID, 1007.5))

    assert set(record) == {
        "platform",
        "url_hash",
        "ok",
        "error_code",
        "cache_hit",
        "probe_ms",
        "download_ms",
        "processing_ms",
        "upload_ms",
        "total_ms",
        "download_bytes",
        "upload_bytes",
        "download_mbps",
        "upload_mbps",
        "queue_wait_ms",
    }


def test_cache_hit_path_reports_the_wait_unchanged() -> None:
    timings = _timings(1000.0, BOOT_ID, 1007.5)
    timings.cache_hit = True
    record = _record(timings)

    assert record["cache_hit"] is True
    assert record["queue_wait_ms"] == 7500.0
