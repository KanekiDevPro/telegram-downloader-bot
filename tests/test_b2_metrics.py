"""Track B2: upload-path + fallback-outcome telemetry, Spotify DEBUG trace.

Upload path is one of uri/stream/cached/none; fallback outcome is
not_needed/used/skipped:<fixed-token>. Both are additive, None-safe, and
never leak URLs. The Spotify selection trace is DEBUG-only and URL-free.
"""

from __future__ import annotations

import logging
from typing import Any

from services import telemetry as telemetry_module


def _timings() -> Any:
    t = telemetry_module.new_timings(received_at=1000.0)
    t.started_at = 1000.0
    t.completed_at = 1020.0
    return t


def test_metrics_default_upload_path_and_fallback() -> None:
    record = telemetry_module.download_metrics_record(
        platform="youtube", url_hash="abc", timings=_timings(), ok=True
    )
    assert record["upload_path"] == "none"
    assert record["fallback_outcome"] == "not_needed"


def test_metrics_accepts_fixed_vocab() -> None:
    for path in ("uri", "stream", "cached", "none"):
        record = telemetry_module.download_metrics_record(
            platform="youtube",
            url_hash="abc",
            timings=_timings(),
            ok=True,
            upload_path=path,
            fallback_outcome="used",
        )
        assert record["upload_path"] == path
        assert record["fallback_outcome"] == "used"
    record = telemetry_module.download_metrics_record(
        platform="youtube",
        url_hash="abc",
        timings=_timings(),
        ok=True,
        fallback_outcome="skipped:quarantined",
    )
    assert record["fallback_outcome"] == "skipped:quarantined"


def test_metrics_sanitizes_hostile_values_none_safe() -> None:
    record = telemetry_module.download_metrics_record(
        platform="youtube",
        url_hash="abc",
        timings=_timings(),
        ok=True,
        upload_path="file:///etc/passwd?sig=SECRET",
        fallback_outcome="skipped:قرنطینه بود (dynamic-reason)",
    )
    assert record["upload_path"] == "none"
    assert record["fallback_outcome"] in ("not_needed", "used", "skipped:unknown", "skipped:quarantined")
    # Must never echo the hostile text.
    import json

    blob = json.dumps(record, ensure_ascii=False, default=str)
    assert "SECRET" not in blob
    assert "قرنطینه" not in blob
    assert "dynamic-reason" not in blob

    none_record = telemetry_module.download_metrics_record(
        platform="youtube",
        url_hash="abc",
        timings=None,
        ok=False,
        error_code="X",
        upload_path=None,
        fallback_outcome=None,
    )
    assert none_record["upload_path"] == "none"
    assert none_record["fallback_outcome"] == "not_needed"


async def test_spotify_debug_trace_is_url_free(
    monkeypatch: Any, caplog: Any
) -> None:
    from services import providers as providers_module
    from services import spotify
    from services.extractor import SearchHit

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
        # Track with a fixed duration so the delta is deterministic.
        from services.spotify import SpotifyTrack

        return SpotifyTrack(
            track_id="tid123",
            title="Song",
            artists=("Artist",),
            duration_s=200,
        )

    monkeypatch.setattr(spotify, "lookup", fake_lookup)

    class _Ext:
        async def search(self, query: str, limit: int = 5) -> list[Any]:
            return [
                SearchHit(
                    url="https://www.youtube.com/watch?v=abc123&sig=SECRET",
                    title="Artist - Song",
                    duration_s=203,
                )
            ]

    with caplog.at_level(logging.DEBUG, logger="services.providers"):
        target = await providers_module.spotify_audio_target(
            "https://open.spotify.com/track/tid123?sig=SECRET", _Ext()  # type: ignore[arg-type]
        )
    assert target.url.startswith("https://")
    debug_text = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )
    # Must describe the selection without any URL material.
    assert "youtube" in debug_text.lower()
    assert "SECRET" not in debug_text
    assert "watch?v=" not in debug_text
    assert "sig=" not in debug_text
    assert "open.spotify" not in debug_text


def test_worker_upload_and_fallback_helpers() -> None:
    # URI vs stream is decided by the same gate _media_ref uses.
    from pathlib import Path

    from services import worker as worker_module

    assert worker_module._upload_path_for(Path("/nope/file.mp4")) in ("uri", "stream")

    from services.extractor import ExtractionError

    class _Svc:
        enabled = True
        available = True
        quarantine_reason = ""

    # Non-block errors never become "skipped".
    assert (
        worker_module._fallback_skip_outcome(
            ExtractionError("PRIVATE_VIDEO", "private"), _Svc()  # type: ignore[arg-type]
        )
        == "not_needed"
    )
    # Blocked + no client -> fixed skipped token, no Persian, no dynamic text.
    assert (
        worker_module._fallback_skip_outcome(
            ExtractionError("EXTRACTOR_BLOCKED", "blocked"), None
        )
        == "skipped:no_client"
    )
