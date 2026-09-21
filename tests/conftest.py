"""Pytest bootstrap: make the project importable no matter where pytest runs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


#: A doctor run asks two *local* helper servers (the PO-token provider and the
#: YouTube session server) and reports what they say. That is the point in
#: production and a hazard in tests: a developer whose stack happens to be up — or
#: a CI host with something on port 4416 — would get different verdicts for the
#: same code, and "no FAIL lines" assertions would flip with the environment.
#: Stub both for every test; the tests that are *about* those probes patch over
#: this (``tests/test_doctor_helpers.py``) and can assert any state they like.
async def _provider_up(url: str, timeout: float = 5.0) -> Any:
    from services.doctor import PotProvider

    return PotProvider(reachable=True, version="2.0.0")


async def _session_ready(url: str, timeout: float = 5.0) -> Any:
    from services.doctor import SessionServer

    return SessionServer(reachable=True, ready=True, age=60.0, token_length=200)


@pytest.fixture(autouse=True)
def helper_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy pair of helpers, whatever this machine is running."""
    from services import doctor as doctor_service

    monkeypatch.setattr(doctor_service, "probe_pot_provider", _provider_up)
    monkeypatch.setattr(doctor_service, "probe_session_server", _session_ready)
