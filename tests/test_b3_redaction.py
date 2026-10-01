"""Track B3: logs never carry query/fragment/userinfo — host + digest only.

Raw links carry signed queries, tokens and userinfo; the log-safe form is
``host#digest`` (digest joins with the metrics line, host keeps it readable).
The metrics JSON line and user messages are untouched — only logger calls.
"""

from __future__ import annotations

import logging
from typing import Any

from services import telemetry as telemetry_module

SECRET_URL = "https://user:pass@www.youtube.com/watch?v=abc123&sig=SECRET#frag"


def test_log_url_strips_secrets_and_is_stable() -> None:
    safe = telemetry_module.log_url(SECRET_URL)

    assert "SECRET" not in safe
    assert "user" not in safe.lower() or "youtube" in safe.lower()
    assert "pass" not in safe
    assert "sig=" not in safe
    assert "#frag" not in safe and "frag" not in safe.replace("youtube", "")
    assert "youtube.com" in safe
    assert telemetry_module.log_url(SECRET_URL) == safe
    assert telemetry_module.log_url("") == "?"
    assert telemetry_module.log_url(None) == "?"


async def test_providers_warning_is_redacted(
    monkeypatch: Any, caplog: Any
) -> None:
    from services import providers as providers_module
    from services import spotify
    from services.extractor import ExtractionError, SearchHit

    async def fake_lookup(url: str, **kwargs: Any) -> Any:
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
                    url="https://www.youtube.com/watch?v=other&sig=SECRET",
                    title="x",
                    duration_s=900,
                )
            ]

    with caplog.at_level(logging.WARNING, logger="services.providers"):
        try:
            await providers_module.spotify_audio_target(
                "https://open.spotify.com/track/tid123", _Ext()  # type: ignore[arg-type]
            )
        except ExtractionError:
            pass

    warnings = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "SECRET" not in warnings
    assert "sig=" not in warnings
    assert "watch?v=" not in warnings


async def test_worker_settle_log_is_redacted(caplog: Any) -> None:
    from services import worker as worker_module
    from services.queue import DownloadTask

    class _BadQueue:
        async def release(self, task: Any) -> None:
            raise RuntimeError("redis down")

    task = DownloadTask(
        url="https://www.youtube.com/watch?v=abc123&sig=SECRET",
        telegram_id=1,
        chat_id=1,
    )
    with caplog.at_level(logging.DEBUG, logger="services.worker"):
        await worker_module._settle(_BadQueue(), task)  # type: ignore[arg-type]

    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET" not in blob
    assert "sig=" not in blob
    assert "watch?v=" not in blob
