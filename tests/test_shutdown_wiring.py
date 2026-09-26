"""Shutdown must tear down everything build_app started — the login child included.

The defect pinned here: ``shutdown`` guards against orphaning an in-flight OAuth
device-flow child ("no login child may outlive the bot that started it") via
``app.get("oauth")`` — but ``build_app`` never put ``oauth`` in the app dict, so
the guard was dead code and a yt-dlp login process started by ``/oauth`` kept
running (polling Google with nobody watching) after every shutdown. The
round-trip is what this test drives: build the app, shut it down, and look for
the teardown of every resource the builder created.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Router

import main as app_module
from core.config import Settings
from services.proxy_health import TunnelHealth


class _Pool:
    async def close(self) -> None:
        return None


class _RecordingOAuth:
    """The login service, remembering whether shutdown stopped it."""

    instances: list["_RecordingOAuth"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.cancelled = False
        self.kwargs = kwargs
        _RecordingOAuth.instances.append(self)

    async def supported(self) -> tuple[bool, str]:
        return False, "not probed in tests"

    async def cancel(self) -> None:
        self.cancelled = True


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        BOT_TOKEN="42:TESTTOKEN",
        QUEUE_BACKEND="memory",
        WORKER_COUNT=0,
        COOKIE_WATCH_INTERVAL_S=0,
        HELPER_WATCH_INTERVAL_S=0,
    )


@pytest.fixture(autouse=True)
def _offline_wiring(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every collaborator that would touch I/O becomes a recorder or a no-op."""

    async def create_pool(*args: Any, **kwargs: Any) -> _Pool:
        return _Pool()

    async def init_db(pool: Any) -> None:
        return None

    async def text_overrides(pool: Any) -> list[Any]:
        return []

    async def resolve_pot_provider(settings: Any) -> str:
        return ""

    async def resolve_tunnel(settings: Any) -> TunnelHealth:
        return TunnelHealth()

    async def run_maintenance(*args: Any, **kwargs: Any) -> None:
        return None  # a real sweep would need a real pool

    async def notify_admins(*args: Any, **kwargs: Any) -> int:
        return 0

    # The global routers may only ever belong to one Dispatcher — give each
    # test's app its own fresh, empty stand-ins.
    monkeypatch.setattr(app_module, "ROUTERS", [Router()])
    monkeypatch.setattr(app_module, "get_settings", _settings)
    monkeypatch.setattr(app_module, "create_pool", create_pool)
    monkeypatch.setattr(app_module, "init_db", init_db)
    monkeypatch.setattr(app_module, "text_overrides", text_overrides)
    monkeypatch.setattr(app_module, "resolve_pot_provider", resolve_pot_provider)
    monkeypatch.setattr(app_module, "resolve_tunnel", resolve_tunnel)
    monkeypatch.setattr(app_module, "run_maintenance", run_maintenance)
    monkeypatch.setattr(app_module, "notify_admins", notify_admins)
    monkeypatch.setattr(app_module, "OAuthService", _RecordingOAuth)
    monkeypatch.setattr(app_module, "build_payment_service", lambda pool: None)
    monkeypatch.setattr(
        app_module.cobalt_cookies,
        "ensure_cookie_dir",
        lambda directory: "",
    )
    monkeypatch.setattr(
        app_module.cobalt_cookies,
        "sync_from_jar",
        lambda *args, **kwargs: SimpleNamespace(
            off=True, usable=False, written=False, source=None, describe=lambda: "off"
        ),
    )
    _RecordingOAuth.instances.clear()
    yield


async def test_shutdown_stops_the_login_child() -> None:
    """The round-trip: whatever ``build_app`` starts, ``shutdown`` stops — and
    the /oauth login child (a real yt-dlp process) is the one that must never
    outlive the bot."""
    app = await app_module.build_app(send_digest=False)
    assert _RecordingOAuth.instances, "the login service is part of the app"
    oauth = _RecordingOAuth.instances[-1]

    await app_module.shutdown(app)

    assert oauth.cancelled, "shutdown must stop the login child — the guard was dead code"


async def test_the_app_exposes_what_shutdown_tears_down() -> None:
    """The contract between the two halves: the app dict carries every resource
    ``shutdown`` looks up. A key missing here is a teardown that never runs."""
    app = await app_module.build_app(send_digest=False)

    assert app.get("oauth") is _RecordingOAuth.instances[-1]
    dp = app["dp"]
    assert dp.error.handlers, "centralized error handling is registered"
