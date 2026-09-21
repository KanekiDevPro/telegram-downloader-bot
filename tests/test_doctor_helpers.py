"""The two helper servers the YouTube routes lean on, as the doctor sees them.

Both checks exist for the same reason: a stack can be *wired* and still be unable
to serve a YouTube link without a login — because the PO-token provider is down, or
because the session server has never produced a token. Neither failure is visible in
a user's message, so the parsing, the states and the wording are pinned here, over a
stand-in for ``aiohttp.ClientSession`` rather than a real port.
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp
import pytest

from core import config as config_module
from core.config import Settings
from services import doctor as doctor_service
from services.doctor import (
    POT_CHECK_NAME,
    SESSION_CHECK_NAME,
    PotProvider,
    SessionServer,
    _host_side_hint,
    _provider_check,
    _seconds_since,
    _session_server_check,
    probe_pot_provider,
    probe_session_server,
)

POT_URL = "http://pot-provider:4416"
SESSION_URL = "http://yt-session-generator:8080"
REFUSED = aiohttp.ClientConnectionError("connection refused")


# ---------------------------------------------------------------------------
# A stand-in for aiohttp.ClientSession (the project's usual style: no sockets)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(
        self, status: int = 200, payload: object = None, error: Exception | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self._error = error

    async def json(self, **_kwargs: Any) -> object:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def __aenter__(self) -> FakeResponse:
        # aiohttp surfaces a refused connection when the response context opens.
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.asked: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.asked.append(url)
        return self._response

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a fake session and hand the test a way to choose its answer."""

    def install(response: FakeResponse) -> FakeSession:
        session = FakeSession(response)
        monkeypatch.setattr(aiohttp, "ClientSession", lambda **_kwargs: session)
        return session

    return install


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The PO-token provider (yt-dlp's own route)
# ---------------------------------------------------------------------------


async def test_the_provider_is_asked_the_question_ytdlp_asks(
    fake_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/ping is the plugin's own availability endpoint — including the version."""
    session = fake_http(FakeResponse(200, {"version": "2.0.0", "server_uptime": 42}))
    # The address a probe uses is translated inside core.config, so that is where
    # "running in a container" has to be true for the service name to survive.
    monkeypatch.setattr(config_module, "in_container", lambda: True)

    provider = await probe_pot_provider(POT_URL)

    assert session.asked == [f"{POT_URL}/ping"]
    assert provider == PotProvider(reachable=True, version="2.0.0")


async def test_a_port_nobody_listens_on_reads_as_unreachable(fake_http: Any) -> None:
    fake_http(FakeResponse(error=REFUSED))

    provider = await probe_pot_provider(POT_URL)

    assert provider.reachable is False
    assert "پاسخ نمی‌دهد" in provider.error


async def test_an_answer_without_a_version_is_not_a_pass(fake_http: Any) -> None:
    fake_http(FakeResponse(500, None))

    provider = await probe_pot_provider(POT_URL)

    assert provider.reachable is True
    assert provider.version == ""
    assert "HTTP 500" in provider.error


def test_a_provider_that_is_off_says_how_to_turn_it_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL="")

    check = _provider_check(settings, None)

    assert check.status == "warn"
    assert "http://pot-provider:4416" in check.detail


def test_the_plugin_version_is_compared_with_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A major mismatch is *silent* otherwise: the plugin just rejects the server."""
    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL=POT_URL)

    drifted = _provider_check(settings, PotProvider(True, "3.1.0"), "2.0.0")
    matched = _provider_check(settings, PotProvider(True, "2.4.1"), "2.0.0")

    assert drifted.status == "warn"
    assert "v3.1.0" in drifted.detail and "v2.0.0" in drifted.detail
    assert "رد" in drifted.detail  # says what the consequence is, not just the fact
    assert matched.status == "ok"
    assert "v2.4.1" in matched.detail


def test_a_provider_without_the_plugin_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL=POT_URL)
    monkeypatch.setattr(doctor_service, "pot_plugin_installed", lambda: False)

    assert _provider_check(settings, PotProvider(True, "2.0.0"), "2.0.0").status == "fail"


def test_a_dead_provider_says_downloads_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot survives without a provider — the report must not read like a crash."""
    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL=POT_URL)
    monkeypatch.setattr(doctor_service, "in_container", lambda: True)

    check = _provider_check(settings, PotProvider(False, error="پاسخ نمی‌دهد"), "2.0.0")

    assert check.status == "fail"
    assert "بدون توکن ادامه پیدا" in check.detail
    assert "docker compose up -d pot-provider" in check.detail


# ---------------------------------------------------------------------------
# The session server (the fallback engine's no-login route)
# ---------------------------------------------------------------------------


def _token_body(*, age: float = 120.0, length: int = 200) -> dict[str, object]:
    return {
        "potoken": "t" * length,
        "visitor_data": "v" * 20,
        "updated": int(time.time() - age),
    }


async def test_a_ready_session_server_hands_over_its_token(
    fake_http: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = fake_http(FakeResponse(200, _token_body(age=120)))
    monkeypatch.setattr(config_module, "in_container", lambda: True)

    server = await probe_session_server(SESSION_URL)

    assert session.asked == [f"{SESSION_URL}/token"]
    assert server.ready is True
    assert server.age is not None and 100 < server.age < 200
    assert server.short is False


async def test_a_server_still_producing_its_first_token_is_not_an_error(
    fake_http: Any,
) -> None:
    """503 with "not yet generated" means alive-and-working, and must read that way."""
    fake_http(FakeResponse(503, None))

    server = await probe_session_server(SESSION_URL)

    assert server.reachable is True
    assert server.ready is False
    assert "ساخته نشده" in server.error


async def test_an_http_error_is_reported_with_its_status(fake_http: Any) -> None:
    fake_http(FakeResponse(502, None))

    server = await probe_session_server(SESSION_URL)

    assert server.ready is False
    assert "HTTP 502" in server.error


async def test_a_body_without_a_token_is_not_taken_for_a_session(fake_http: Any) -> None:
    fake_http(FakeResponse(200, {"updated": int(time.time())}))

    server = await probe_session_server(SESSION_URL)

    assert server.ready is False
    assert "potoken" in server.error


async def test_a_warming_server_is_a_warning_that_points_at_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER=SESSION_URL)

    check = _session_server_check(
        settings, SessionServer(True, error="توکن هنوز ساخته نشده — مرورگر در حال تولید است")
    )

    assert check.status == "warn"
    assert "logs yt-session-generator" in check.detail


def test_a_ready_session_names_the_reload_it_will_get(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER=SESSION_URL)

    check = _session_server_check(
        settings, SessionServer(True, ready=True, age=60.0, token_length=200)
    )

    assert check.status == "ok"
    assert check.icon == "🎫"
    assert "توکن آماده" in check.detail
    # Cobalt reloads on its own — telling an admin to restart would be wrong.
    assert "۵ دقیقه" in check.detail


def test_a_short_token_is_named_even_when_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER=SESSION_URL)

    check = _session_server_check(
        settings, SessionServer(True, ready=True, token_length=100)
    )

    assert check.status == "ok"
    assert "کوتاه" in check.detail and "100" in check.detail


def test_an_absent_session_server_is_off_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, YOUTUBE_SESSION_SERVER="")

    check = _session_server_check(settings, None)

    assert check.status == "warn"
    assert "خاموش" in check.detail


def test_milliseconds_are_not_mistaken_for_centuries() -> None:
    """The reference generator stamps seconds; a server that stamps ms must still read right."""
    assert _seconds_since(int((time.time() - 60) * 1000)) is not None
    age = _seconds_since(int((time.time() - 60) * 1000))
    assert age is not None and 50 < age < 70
    assert _seconds_since("nonsense") is None
    assert _seconds_since(None) is None
    assert _seconds_since(int(time.time() + 500)) is None  # a future stamp is not an age


# ---------------------------------------------------------------------------
# "Unreachable" on the wrong machine
# ---------------------------------------------------------------------------


def test_the_hint_names_the_published_port_and_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_service, "in_container", lambda: False)

    hint = _host_side_hint(POT_URL)

    assert "127.0.0.1:4416" in hint
    assert "YTDLP_POT_PROVIDER_URL" in hint
    assert "127.0.0.1:8080" in _host_side_hint(SESSION_URL)


def test_inside_the_container_the_service_name_is_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor_service, "in_container", lambda: True)

    assert _host_side_hint(POT_URL) == ""
    assert _host_side_hint("http://example.com:4416") == ""


# ---------------------------------------------------------------------------
# And a line for each in the report an admin actually reads
# ---------------------------------------------------------------------------


async def test_the_report_carries_both_helper_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _provider_up(url: str, timeout: float = 5.0) -> PotProvider:
        return PotProvider(True, "2.0.0")

    async def _session_ready(url: str, timeout: float = 5.0) -> SessionServer:
        return SessionServer(True, ready=True, age=30.0, token_length=200)

    monkeypatch.setattr(doctor_service, "probe_pot_provider", _provider_up)
    monkeypatch.setattr(doctor_service, "probe_session_server", _session_ready)
    settings = _settings(
        monkeypatch,
        YTDLP_POT_PROVIDER_URL=POT_URL,
        YOUTUBE_SESSION_SERVER=SESSION_URL,
        COOKIE_FILE="nonexistent-cookies.txt",
    )

    report = await doctor_service.run_youtube_doctor(settings, _extractor(), probe=False)
    rendered = report.render()
    statuses = {check.name: check.status for check in report.checks}

    assert statuses[POT_CHECK_NAME] == "ok"
    assert statuses[SESSION_CHECK_NAME] == "ok"
    assert f"✅ {POT_CHECK_NAME}" in rendered
    assert f"🎫 {SESSION_CHECK_NAME}" in rendered


def _extractor() -> Any:
    from pathlib import Path

    from services.extractor import ExtractorService

    return ExtractorService(Path("."), cookie_file=None, js_runtime="none")


def test_the_dead_helper_lines_say_which_setting_to_look_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`COBALT_API_URL` was the only setting the hint knew; every helper has one now."""
    monkeypatch.setattr(doctor_service, "in_container", lambda: False)

    for url, setting in (
        (POT_URL, "YTDLP_POT_PROVIDER_URL"),
        (SESSION_URL, "YOUTUBE_SESSION_SERVER"),
        ("http://cobalt:9000", "COBALT_API_URL"),
    ):
        assert setting in _host_side_hint(url)
