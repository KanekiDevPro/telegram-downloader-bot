"""The session generator's browser, and the one argument that puts it on the tunnel.

``yt-session-generator`` mints YouTube's ``po_token`` with a real Chromium, and no
configuration of that image reaches the browser: the generator calls
``nodriver.start(...)`` without proxy support and Chromium ignores the proxy
environment. This module is mounted as ``sitecustomize`` (imported by Python itself
before any user code) purely so that ``nodriver.start`` can be wrapped and the
switched argument added.

What is worth pinning here is the *decision*: the proxy is forced whenever it can work
(that is the route the deployment asked for, and the only one in WARP's proxy mode),
it is skipped when the proxy is silent (a browser pointed at a dead proxy has no
internet at all, while the shared namespace may still be carrying it), and the
``always``/``never`` modes mean exactly what an operator would expect. The report file
is the second half: it is how the answer reaches ``/doctor``, so a missing or broken
one has to stay harmless and never take the browser's route with it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "session_proxy" / "sitecustomize.py"

LOADED_AS = "session_proxy_sitecustomize"

TRACE = "fl=1\nip=198.51.100.7\nwarp=on\nts=1700000000.0\n"


def _load() -> ModuleType:
    """Import the injector by path — it is a deployment file, not a package module."""
    spec = importlib.util.spec_from_file_location(LOADED_AS, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[LOADED_AS] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def injector() -> ModuleType:
    """The module, with the environment an enabled deployment would give it."""
    return _load()


@pytest.fixture
def report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "browser-route.json"
    monkeypatch.setenv("YT_SESSION_ROUTE_FILE", str(path))
    return path


def _measurable(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    warp: str = "on",
    exit_ip: str = "198.51.100.7",
    proxy_up: bool = True,
) -> None:
    """Replace the two network questions with answers (the probes are exercised below)."""
    monkeypatch.setattr(
        module, "read_trace", lambda *a, **kw: module.Trace(ok=True, warp=warp, exit_ip=exit_ip)
    )
    monkeypatch.setattr(module, "proxy_answers", lambda *a, **kw: proxy_up)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_the_proxy_is_forced_while_it_answers(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route the deployment asked for, taken whenever it can work."""
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    _measurable(monkeypatch, injector)

    decision = injector.decide()

    assert decision.force is True
    assert decision.browser_arg == "--proxy-server=socks5://127.0.0.1:1080"
    assert decision.reason == "proxy-up"
    assert (decision.direct_warp, decision.exit_ip) == ("on", "198.51.100.7")


def test_a_silent_proxy_is_not_pushed_into_the_browser(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser pointed at a dead proxy has no route at all — worse than direct."""
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.delenv("YT_SESSION_PROXY_MODE", raising=False)
    _measurable(monkeypatch, injector, proxy_up=False)

    decision = injector.decide()

    assert decision.force is False
    assert decision.browser_arg is None
    assert decision.reason == "proxy-down", "the report must say why no proxy was set"


def test_always_forces_even_a_silent_proxy(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``always`` is the operator's literal instruction, and it is honoured."""
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://host:1080")
    monkeypatch.setenv("YT_SESSION_PROXY_MODE", "always")
    _measurable(monkeypatch, injector, proxy_up=False)

    decision = injector.decide()

    assert decision.browser_arg == "--proxy-server=socks5://host:1080"
    assert decision.reason == "forced"


def test_never_installs_nothing(injector: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("YT_SESSION_PROXY_MODE", "never")

    decision = injector.decide()

    assert (decision.enabled, decision.force) == (False, False)
    assert decision.reason == "disabled"


def test_an_empty_proxy_setting_means_direct(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly empty value is a choice (run direct), not a missing default."""
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "")
    monkeypatch.delenv("YT_SESSION_PROXY_MODE", raising=False)

    decision = injector.decide()

    assert (decision.enabled, decision.force) == (False, False)
    assert decision.reason == "no-proxy-configured"


def test_the_default_proxy_is_the_tunnel_in_the_shared_namespace(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named ``127.0.0.1`` on purpose: the namespace makes the tunnel local."""
    monkeypatch.delenv("YT_SESSION_CHROMIUM_PROXY", raising=False)
    monkeypatch.delenv("YT_SESSION_PROXY_MODE", raising=False)
    _measurable(monkeypatch, injector)

    decision = injector.decide()

    assert decision.proxy == "socks5://127.0.0.1:1080"
    assert decision.browser_arg.endswith("socks5://127.0.0.1:1080")


def test_an_unreadable_trace_is_not_read_as_direct(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trace that cannot be read is reported as unknown, never as ``warp=off``."""
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    _measurable(monkeypatch, injector)
    monkeypatch.setattr(injector, "read_trace", lambda *a, **kw: injector.Trace())

    decision = injector.decide()

    assert (decision.direct_warp, decision.exit_ip) == ("", "")
    assert decision.force is True, "an unknown exit is not a reason to skip the proxy"


def test_an_unknown_mode_falls_back_to_auto(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YT_SESSION_PROXY_MODE", "PROXY?")

    assert injector.proxy_mode() == "auto"


def test_a_dead_trace_endpoint_is_reported_as_trace_failure(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe itself is exercised here (offline: a closed port on the loopback)."""
    monkeypatch.setenv("YT_SESSION_TRACE_URL", "http://127.0.0.1:9/trace")

    trace = injector.read_trace("http://127.0.0.1:9/trace", timeout=1.0)

    assert (trace.ok, trace.tunnelled) == (False, False)


def test_the_trace_is_parsed_into_warp_and_exit_ip(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return TRACE.encode()

    monkeypatch.setattr(injector.urllib.request, "urlopen", lambda *a, **kw: Response())

    trace = injector.read_trace("https://cloudflare.com/cdn-cgi/trace")

    assert (trace.ok, trace.warp, trace.exit_ip) == (True, "on", "198.51.100.7")
    assert trace.tunnelled is True


def test_a_warp_off_trace_is_not_a_tunnel(injector: ModuleType) -> None:
    assert injector.Trace(ok=True, warp="off").tunnelled is False


# ---------------------------------------------------------------------------
# Getting the argument into nodriver's call
# ---------------------------------------------------------------------------


class _FakeNodriver:
    """The module the generator imports, in the shape the injector expects."""

    __version__ = "0.32"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "browser"


def _fake(monkeypatch: pytest.MonkeyPatch, injector: ModuleType) -> _FakeNodriver:
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    _measurable(monkeypatch, injector)
    return _FakeNodriver()


async def test_a_launch_carries_the_proxy_argument(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    module = _fake(monkeypatch, injector)
    assert injector.install(module) is True

    await module.start(headless=False, user_data_dir="/tmp/profile")

    assert module.calls[0]["browser_args"] == ["--proxy-server=socks5://127.0.0.1:1080"]
    assert json.loads(report.read_text(encoding="utf-8"))["applied"] == "kwargs"


async def test_the_callers_own_arguments_are_kept(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    module = _fake(monkeypatch, injector)
    injector.install(module)

    await module.start(browser_args=["--window-size=1280,720"])

    assert module.calls[0]["browser_args"] == [
        "--proxy-server=socks5://127.0.0.1:1080",
        "--window-size=1280,720",
    ]


async def test_a_proxy_the_caller_set_is_not_overridden(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """Someone who set their own ``--proxy-server`` meant it."""
    module = _fake(monkeypatch, injector)
    injector.install(module)

    await module.start(browser_args=["--proxy-server=http://elsewhere:8080"])

    assert module.calls[0]["browser_args"] == ["--proxy-server=http://elsewhere:8080"]
    assert json.loads(report.read_text(encoding="utf-8"))["applied"] == "caller"


async def test_a_config_object_is_patched_instead_of_the_ignored_keyword(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """``start(config=...)`` *ignores* the keyword arguments — the silent trap."""
    module = _fake(monkeypatch, injector)
    injector.install(module)

    class Config:
        def __init__(self) -> None:
            self.arguments: list[str] = []

        @property
        def browser_args(self) -> list[str]:
            return self.arguments

        def add_argument(self, arg: str) -> None:
            self.arguments.append(arg)

    config = Config()
    await module.start(config)

    assert config.arguments == ["--proxy-server=socks5://127.0.0.1:1080"]
    assert "browser_args" not in module.calls[0]
    assert json.loads(report.read_text(encoding="utf-8"))["applied"] == "config"


async def test_a_launch_without_a_usable_proxy_sets_no_argument(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    module = _fake(monkeypatch, injector)
    _measurable(monkeypatch, injector, proxy_up=False)
    injector.install(module)

    await module.start(headless=False)

    assert module.calls[0] == {"headless": False}
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert (payload["force"], payload["reason"]) == (False, "proxy-down")


async def test_installing_twice_does_not_stack_wrappers(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    module = _fake(monkeypatch, injector)
    injector.install(module)
    original = module.start

    assert injector.install(module) is True
    assert module.start is original


def test_a_module_without_start_is_reported_as_unpatchable(
    injector: ModuleType,
) -> None:
    assert injector.install(object()) is False


async def test_main_refuses_to_leave_the_browser_unproxied(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a silent fallback to the host IP is the bug, so it is fatal."""
    monkeypatch.setenv("YT_SESSION_PROXY_MODE", "auto")
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setitem(sys.modules, "nodriver", _FakeNodriver())
    monkeypatch.setattr(injector, "install", lambda module: False)

    with pytest.raises(SystemExit):
        injector.main()


def test_main_installs_on_a_nodriver_that_can_be_wrapped(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YT_SESSION_CHROMIUM_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setenv("YT_SESSION_ROUTE_FILE", "/nonexistent/dir/route.json")
    module = _FakeNodriver()
    monkeypatch.setitem(sys.modules, "nodriver", module)

    injector.main()

    assert getattr(module.start, "_tunnel_proxy_patched", False) is True


def test_a_disabled_deployment_reports_its_configuration_without_patching(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """``never`` still leaves a report, so \"off\" and \"never launched\" differ."""
    monkeypatch.setenv("YT_SESSION_PROXY_MODE", "never")
    monkeypatch.setenv("YT_SESSION_ROUTE_FILE", str(report))

    injector.main()

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert (payload["enabled"], payload["applied"]) == (False, "startup")


# ---------------------------------------------------------------------------
# The report file
# ---------------------------------------------------------------------------


def test_the_report_carries_the_evidence_and_the_time(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    _measurable(monkeypatch, injector, warp="plus", exit_ip="203.0.113.9")

    injector.write_report(str(report), injector.decide().payload(version="0.32"))

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["direct_warp"] == "plus"
    assert payload["exit_ip"] == "203.0.113.9"
    assert payload["nodriver"] == "0.32"
    assert isinstance(payload["updated"], int) and payload["updated"] > 0


def test_an_unwritable_report_does_not_raise(
    injector: ModuleType, tmp_path: Path
) -> None:
    """The file is how the finding travels; it must never decide the route."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    injector.write_report(str(blocker / "route.json"), {"force": True})


def test_an_empty_report_path_writes_nothing(
    injector: ModuleType, tmp_path: Path
) -> None:
    injector.write_report("", {"force": True})

    assert list(tmp_path.iterdir()) == []
