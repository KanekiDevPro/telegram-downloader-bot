"""Pytest bootstrap: make the project importable no matter where pytest runs."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


#: A doctor run asks three *local* things — the PO-token provider, the YouTube
#: session server, and the WARP tunnel every download leaves through — and reports
#: what they say. That is the point in production and a hazard in tests: a developer
#: whose stack happens to be up (or a CI host with something on port 4416, or a
#: `YTDLP_PROXY` pointing at a machine that is really listening) would get different
#: verdicts for the same code, and "no FAIL lines" assertions would flip with the
#: environment. Stub all three for every test; the tests that are *about* those
#: probes patch over this (``tests/test_doctor_helpers.py``, ``tests/test_proxy_health.py``,
#: ``tests/test_doctor.py``) and can assert any state they like.
async def _provider_up(url: str, timeout: float = 5.0) -> Any:
    from services.doctor import PotProvider

    return PotProvider(reachable=True, version="2.0.0")


async def _session_ready(url: str, timeout: float = 5.0) -> Any:
    from services.doctor import SessionServer

    return SessionServer(reachable=True, ready=True, age=60.0, token_length=200)


async def _tunnel_up(url: str, timeout: float = 5.0) -> Any:
    from services.proxy_health import TunnelHealth

    return TunnelHealth(
        url=url, reachable=True, traced=True, warp="on", exit_ip="203.0.113.9"
    )


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    """Tests must not inherit each other's settings cache.

    ``get_settings`` is an ``lru_cache`` reading env at build time, and tests set
    env before asking for settings — or warm the cache as a side effect (the
    timezone helpers call it). Without this, a warm cache answers with an earlier
    test's ``ADMIN_IDS``/``TIMEZONE``/… and later tests assert against another
    suite's configuration. Individual tests that build the cache deliberately
    (``tests/test_fallback.py``, ``tests/test_queue.py``) already clear it at both
    ends; this makes that courtesy universal.
    """
    from core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def helper_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy helper stack, whatever this machine is running."""
    from services import doctor as doctor_service

    monkeypatch.setattr(doctor_service, "probe_pot_provider", _provider_up)
    monkeypatch.setattr(doctor_service, "probe_session_server", _session_ready)
    monkeypatch.setattr(doctor_service, "probe_tunnel", _tunnel_up)
