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


async def test_boot_warms_the_catalogue_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_app`` starts the warm-up and keeps booting: awaiting it would
    only move the wait into the boot log. And the task is held like every other
    background task — one collected mid-flight warms nothing at all."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_warm() -> None:
        started.set()
        await gate.wait()

    monkeypatch.setattr(app_module, "warm_extractor_catalogue", slow_warm)

    app: dict[str, Any] | None = None
    try:
        # The timeout is the assertion: build_app must return while the warm-up
        # is still held behind the gate.
        app = await asyncio.wait_for(app_module.build_app(send_digest=False), timeout=5)
        await asyncio.wait_for(started.wait(), timeout=5)
        assert any(
            task.get_name() == "extractor-warmup" for task in app["workers"]
        ), "the warm-up is held — and drained at shutdown — like every other task"
    finally:
        gate.set()
        if app is not None:
            await asyncio.gather(*app["workers"], return_exceptions=True)
