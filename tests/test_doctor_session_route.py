"""The session generator's browser: which route it took, as ``/doctor`` reports it.

``yt-session-generator`` mints YouTube's ``po_token`` with a real Chromium, and that
browser is the one process in this stack no environment variable reaches — it ignores
``HTTP_PROXY``, and the image calls ``nodriver.start`` with no proxy support. So the
settings can read perfectly while the browser still leaves from this host's flagged
address, which is why the deployment forces the argument in
(``deploy/session_proxy/``) and why the result is *read back* here.

What is pinned below is the reading and the verdict: a report that says the browser was
sent through the proxy is only good news when something also shows the tunnel is a
tunnel — either a direct request from that namespace reporting ``warp=on``, or the
bot's own probe of the same proxy reporting where its traffic surfaces. The failure
that must never read as healthy is a forced proxy that is *not* WARP: the browser then
leaves from the address YouTube already refuses.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.config import Settings
from services import doctor as doctor_service
from services.doctor import (
    ROUTE_CHECK_NAME,
    SessionRoute,
    _base_checks,
    _session_route_check,
    read_session_route,
)
from services.extractor import ExtractorService
from services.proxy_health import TunnelHealth

PROXY = "socks5://127.0.0.1:1080"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _route(**overrides: object) -> SessionRoute:
    base: dict[str, object] = {
        "enabled": True,
        "mode": "auto",
        "proxy": PROXY,
        "force": True,
        "proxy_up": True,
        "direct_warp": "",
        "exit_ip": "",
        "reason": "proxy-up",
        "applied": "kwargs",
        "age": 5.0,
    }
    base.update(overrides)
    return SessionRoute(**base)  # type: ignore[arg-type]


def _tunnel(**overrides: object) -> TunnelHealth:
    base: dict[str, object] = {
        "url": "http://warp:1080",
        "reachable": True,
        "traced": True,
        "warp": "on",
        "exit_ip": "198.51.100.7",
        "detail": "HTTP 200",
    }
    base.update(overrides)
    return TunnelHealth(**base)  # type: ignore[arg-type]


def _report(tmp_path: Path, **fields: object) -> Path:
    path = tmp_path / "browser-route.json"
    payload: dict[str, object] = {
        "enabled": True,
        "mode": "auto",
        "proxy": PROXY,
        "force": True,
        "proxy_up": True,
        "direct_warp": "on",
        "exit_ip": "198.51.100.7",
        "reason": "proxy-up",
        "applied": "kwargs",
        "updated": int(time.time()) - 10,
    }
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading the report
# ---------------------------------------------------------------------------


def test_nothing_is_read_when_the_setting_is_off() -> None:
    assert read_session_route(None) is None


def test_a_missing_report_is_not_an_error(tmp_path: Path) -> None:
    assert read_session_route(tmp_path / "absent.json") is None


def test_a_half_written_report_is_treated_as_missing(tmp_path: Path) -> None:
    """The writer renames into place, so a truncated file is the worst case."""
    path = tmp_path / "browser-route.json"
    path.write_text('{"enabled": true, "proxy":', encoding="utf-8")

    assert read_session_route(path) is None


def test_a_report_that_is_not_an_object_is_treated_as_missing(tmp_path: Path) -> None:
    path = tmp_path / "browser-route.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert read_session_route(path) is None


def test_the_report_is_read_with_its_age(tmp_path: Path) -> None:
    path = _report(tmp_path, direct_warp="plus", exit_ip="203.0.113.9")

    route = read_session_route(path)

    assert route is not None
    assert (route.direct_warp, route.exit_ip, route.applied) == (
        "plus",
        "203.0.113.9",
        "kwargs",
    )
    assert route.age is not None and 0 <= route.age < 120
    assert route.stale is False


def test_an_old_report_is_stale(tmp_path: Path) -> None:
    """A verdict about a browser that has since been relaunched is not current."""
    path = _report(tmp_path, updated=int(time.time()) - 3 * 3600)

    route = read_session_route(path)

    assert route is not None and route.stale is True


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------


def test_no_report_is_reported_with_the_command_that_would_produce_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, YT_SESSION_ROUTE_FILE="/runtime/browser-route.json")

    check = _session_route_check(settings, None, None)

    assert check.status == "warn"
    assert "yt-session-generator" in check.detail, "the fix has to name the command"


def test_a_switched_off_route_says_which_mode_it_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(enabled=False, force=False, reason="disabled"),
        None,
    )

    assert check.status == "warn"
    assert "never" in check.detail


def test_a_forced_proxy_plus_a_tunnelled_namespace_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both routes are WARP here, so the browser cannot be on the host address."""
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(direct_warp="on", exit_ip="198.51.100.7"),
        _tunnel(),
    )

    assert check.status == "ok"
    assert "198.51.100.7" in check.detail


def test_a_forced_proxy_is_confirmed_by_the_proxys_own_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proxy-mode client: direct traffic is not tunnelled, the proxy has to carry it."""
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(direct_warp="off"),
        _tunnel(warp="plus", exit_ip="203.0.113.9"),
    )

    assert check.status == "ok"
    assert "plus" in check.detail and "203.0.113.9" in check.detail


def test_a_forced_proxy_that_is_not_warp_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one reading that must never look healthy: a browser on the blocked address."""
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(direct_warp="off"),
        _tunnel(warp="off"),
    )

    assert check.status == "fail"
    assert "GOST_ARGS" in check.detail, "the fix for a proxy-mode client has to be named"


def test_an_unconfirmed_exit_is_a_warning_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)

    check = _session_route_check(settings, _route(direct_warp="off"), None)

    assert check.status == "warn"
    assert "تأیید نشد" in check.detail


def test_a_silent_proxy_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No argument was injected, so the browser is on whatever the namespace does."""
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(force=False, proxy_up=False, reason="proxy-down"),
        None,
    )

    assert check.status == "warn"
    assert PROXY in check.detail
    assert "warp" in check.detail, "the namespace may still be tunnelled, and it says so"


def test_a_caller_supplied_proxy_is_named_in_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the generator set its own proxy, the row must not claim we did."""
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(applied="caller", direct_warp="on", exit_ip="198.51.100.7"),
        _tunnel(),
    )

    assert "خودِ مولد" in check.detail


def test_a_stale_report_never_reads_as_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)

    check = _session_route_check(
        settings,
        _route(direct_warp="on", age=doctor_service.SESSION_ROUTE_STALE_S + 60),
        _tunnel(),
    )

    assert check.status == "warn"
    assert "دوباره اجرا نشده" in check.detail


def test_the_reason_is_translated_and_an_unknown_one_is_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)

    known = _session_route_check(settings, _route(reason="forced", direct_warp="on"), _tunnel())
    unknown = _session_route_check(
        settings, _route(reason="something-new", direct_warp="on"), _tunnel()
    )

    assert "always" in known.detail
    assert "something-new" in unknown.detail


# ---------------------------------------------------------------------------
# Where the row sits in the report
# ---------------------------------------------------------------------------


def _extractor(tmp_path: Path) -> ExtractorService:
    return ExtractorService(tmp_path, cookie_file=None, js_runtime="none")


def test_the_row_is_absent_when_no_report_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch)

    checks = _base_checks(settings, _extractor(tmp_path), None, None, None, None, None)

    assert ROUTE_CHECK_NAME not in [check.name for check in checks]


def test_the_row_sits_next_to_the_session_server_it_describes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Order matters: the browser's route reads as part of the fallback's session."""
    settings = _settings(monkeypatch, YT_SESSION_ROUTE_FILE="/runtime/browser-route.json")

    checks = _base_checks(
        settings,
        _extractor(tmp_path),
        None,
        None,
        None,
        _tunnel(),
        _route(direct_warp="on"),
    )

    names = [check.name for check in checks]
    assert names.index(ROUTE_CHECK_NAME) == names.index(doctor_service.SESSION_CHECK_NAME) + 1
