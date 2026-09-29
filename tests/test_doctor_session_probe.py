"""The /doctor session row: three states, one bounded probe.

The session server is the fallback engine's no-login route for YouTube, and
/doctor's row about it must keep three facts apart: the operator never switched
it on (off — not a failure), it answers but has no token yet (503 warming —
a warning, because the browser takes minutes), and it answers with one (ok).
A slow-but-healthy generator must never read as broken, and a dead one must
never hang the doctor — so the probe's own ceiling is 3 s, and the doctor
spends exactly one probe on this row.
"""

from __future__ import annotations

import inspect
from typing import Any

import aiohttp
import pytest

from core import config as config_module
from core.config import Settings
from services import doctor as doctor_service
from services.doctor import (
    SESSION_CHECK_NAME,
    SessionServer,
    _session_server_check,
    probe_session_server,
)

SESSION_URL = "http://yt-session-generator:8080"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_session_probe_gives_up_after_three_seconds() -> None:
    """A dead generator must not hang /doctor; a slow-but-healthy one must not
    read as broken. The probe's own ceiling is 3 s, not 1 s and not 5 s."""
    default = inspect.signature(probe_session_server).parameters["timeout"].default
    assert default == 3.0


async def test_the_probe_hands_its_ceiling_to_the_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is real only if it reaches the socket: the 3 s ceiling must be
    the total timeout of the request itself, so an unresponsive generator reads
    as unreachable instead of hanging the doctor."""
    seen: dict[str, Any] = {}

    class CapturingResponse:
        status = 503

        async def json(self, **_kwargs: Any) -> object:
            raise ValueError("not json")

        async def __aenter__(self) -> CapturingResponse:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class CapturingSession:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        def get(self, _url: str) -> CapturingResponse:
            return CapturingResponse()

        async def __aenter__(self) -> CapturingSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kwargs: CapturingSession(**kwargs))
    monkeypatch.setattr(config_module, "in_container", lambda: True)

    server = await probe_session_server(SESSION_URL)

    assert server.reachable is True  # 503 warming: alive, still producing
    timeout = seen.get("timeout")
    assert timeout is not None and timeout.total == 3.0


def test_a_configured_but_tokenless_generator_is_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 warming: alive and working on its first token — a warning that points
    at the log, never a failure."""
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER=SESSION_URL)

    check = _session_server_check(
        settings, SessionServer(True, error="توکن هنوز ساخته نشده — مرورگر در حال تولید است")
    )

    assert check.name == SESSION_CHECK_NAME
    assert check.status == "warn"
    assert "logs yt-session-generator" in check.detail


def test_an_unconfigured_generator_is_off_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No setting, no row to fail: off is a warning in the operator's language."""
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER="")

    check = _session_server_check(settings, None)

    assert check.name == SESSION_CHECK_NAME
    assert check.status == "warn"
    assert "خاموش" in check.detail


def test_an_answering_generator_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER=SESSION_URL)

    check = _session_server_check(
        settings, SessionServer(True, ready=True, age=60.0, token_length=200)
    )

    assert check.name == SESSION_CHECK_NAME
    assert check.status == "ok"
    assert "توکن آماده" in check.detail


async def test_the_doctor_spends_one_bounded_probe_on_the_session_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One probe, one row: /doctor asks the generator once, with the 3 s ceiling."""
    calls: list[tuple[str, float]] = []

    async def recording_probe(url: str, timeout: float = 3.0) -> SessionServer:
        calls.append((url, timeout))
        return SessionServer(True, ready=True, age=30.0, token_length=200)

    monkeypatch.setattr(doctor_service, "probe_session_server", recording_probe)
    settings = _settings(
        monkeypatch,
        YOUTUBE_SESSION_SERVER=SESSION_URL,
        COOKIE_FILE="nonexistent-cookies.txt",
    )

    from pathlib import Path

    from services.extractor import ExtractorService

    extractor = ExtractorService(Path("."), cookie_file=None, js_runtime="none")
    report = await doctor_service.run_youtube_doctor(settings, extractor, probe=False)
    statuses = {check.name: check.status for check in report.checks}

    assert statuses[SESSION_CHECK_NAME] == "ok"
    assert len(calls) == 1, "one probe per /doctor run — never a retry loop"
    assert calls[0][1] == 3.0, "the doctor spends the bounded probe, not the old 5 s"
