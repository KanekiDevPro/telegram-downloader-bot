"""The first link must never pay yt-dlp's cold extractor import.

The defect pinned here: yt-dlp builds its site catalogue by importing every
extractor module the first time anyone asks for one (``gen_extractors``) — a
multi-second import charged to whichever call came first. That was the user's
very first message: seconds of silence for an answer no later link pays for.
The startup lifecycle pays it instead — a background task warms the catalogue
on a worker thread while boot continues — so every link is a warm call and the
loop never freezes behind the import.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

# The build_app harness from the module that owns it (every collaborator
# offline). A fixture is a module attribute, so importing one applies it here
# exactly as it applies there.
from test_shutdown_wiring import _offline_wiring as _offline_wiring  # noqa: F401

import main as app_module
from core.config import Settings


async def test_warming_the_catalogue_never_runs_on_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``is_url_supported`` reads yt-dlp's whole catalogue — the first call is
    the import. Warming it ties up a worker thread for seconds, never the loop
    that serves every chat meanwhile."""
    loop_thread = threading.get_ident()
    warmed: list[int] = []

    def probe(url: str) -> bool:
        warmed.append(threading.get_ident())
        return True

    monkeypatch.setattr(
        app_module.ExtractorService, "is_url_supported", staticmethod(probe)
    )

    await app_module.warm_extractor_catalogue()

    assert warmed and warmed[0] != loop_thread, (
        "the catalogue is imported on a worker thread, never on the loop"
    )


def _settings_with_workers() -> Settings:
    """The offline wiring's settings with real workers to order — the shared
    fixture boots zero of them, and this defect lives in the ordering."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        BOT_TOKEN="42:TESTTOKEN",
        QUEUE_BACKEND="memory",
        WORKER_COUNT=2,
        COOKIE_WATCH_INTERVAL_S=0,
        HELPER_WATCH_INTERVAL_S=0,
    )


async def test_boot_holds_until_the_catalogue_is_warm_and_only_then_starts_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The warm-up "serves the first request" used to be a hope, not a rule: the
    workers started in the same tick, so a job that dequeued during the cold
    import paid it again in its own thread — two threads importing yt-dlp's
    extractor catalogue at once. Boot holds until the warm-up lands now, so no
    worker ever starts against a cold catalogue. The task is still held like
    every other background task — one collected mid-flight warms nothing."""
    gate = asyncio.Event()
    started = asyncio.Event()
    warm: list[bool] = []
    done = False

    async def slow_warm() -> None:
        nonlocal done
        started.set()
        await gate.wait()
        done = True

    async def fake_worker(*args: Any) -> None:
        warm.append(done)

    monkeypatch.setattr(app_module, "warm_extractor_catalogue", slow_warm)
    monkeypatch.setattr(app_module, "run_worker", fake_worker)
    monkeypatch.setattr(app_module, "get_settings", _settings_with_workers)

    boot = asyncio.create_task(app_module.build_app(send_digest=False))
    app: dict[str, Any] | None = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not boot.done(), "boot must wait for the warm-up before the workers start"
        gate.set()
        app = await asyncio.wait_for(boot, timeout=5)
        await asyncio.sleep(0)
        assert warm and all(warm), "no worker may ever start against a cold catalogue"
        assert any(
            task.get_name() == "extractor-warmup" for task in app["workers"]
        ), "the warm-up is held — and drained at shutdown — like every other task"
    finally:
        gate.set()
        await asyncio.gather(boot, return_exceptions=True)
        if app is not None:
            await asyncio.gather(*app["workers"], return_exceptions=True)
