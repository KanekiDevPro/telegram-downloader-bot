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
import os
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


@pytest.fixture(autouse=True)
def _no_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``$DISPLAY`` unless a test asks for one.

    The display repair acts on whatever ``$DISPLAY`` says, so a suite run on a
    machine with a broken display set must not let it spawn a real Xvfb mid-test.
    """
    monkeypatch.delenv("DISPLAY", raising=False)


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

    assert module.calls[0]["browser_args"] == [
        "--proxy-server=socks5://127.0.0.1:1080",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
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
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]


async def test_a_proxy_the_caller_set_is_not_overridden(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """Someone who set their own ``--proxy-server`` meant it."""
    module = _fake(monkeypatch, injector)
    injector.install(module)

    await module.start(browser_args=["--proxy-server=http://elsewhere:8080"])

    assert module.calls[0]["browser_args"] == [
        "--proxy-server=http://elsewhere:8080",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
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
            self.sandbox: bool | None = None

        @property
        def browser_args(self) -> list[str]:
            return self.arguments

        def add_argument(self, arg: str) -> None:
            self.arguments.append(arg)

    config = Config()
    await module.start(config)

    assert config.arguments == [
        "--proxy-server=socks5://127.0.0.1:1080",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]
    assert config.sandbox is False
    assert "browser_args" not in module.calls[0]
    assert "sandbox" not in module.calls[0]
    assert json.loads(report.read_text(encoding="utf-8"))["applied"] == "config"


async def test_a_launch_without_a_usable_proxy_keeps_the_browser_direct(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """No proxy is a routing decision; the root-safe defaults are not optional."""
    module = _fake(monkeypatch, injector)
    _measurable(monkeypatch, injector, proxy_up=False)
    injector.install(module)

    await module.start(headless=False)

    args = module.calls[0].get("browser_args", [])
    assert not any(item.startswith("--proxy-server") for item in args)
    assert module.calls[0]["sandbox"] is False
    assert args == ["--disable-setuid-sandbox", "--disable-dev-shm-usage"]
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


# ---------------------------------------------------------------------------
# The display the browser needs
# ---------------------------------------------------------------------------


def _display_env(monkeypatch: pytest.MonkeyPatch, module: ModuleType, tmp_path: Path) -> Path:
    """Point the repair at a scratch directory and ask it for a display."""
    monkeypatch.setattr(module, "X_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":99")
    return tmp_path


def test_a_healthy_display_is_left_alone(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The common case is one probe and no action — a repair must not fidget."""
    _display_env(monkeypatch, injector, tmp_path)
    spawns: list[int] = []
    monkeypatch.setattr(injector, "probe_display", lambda path: True)
    monkeypatch.setattr(injector, "spawn_xvfb", spawns.append)

    assert injector.ensure_x_display() == "ok"
    assert spawns == []


def test_a_stale_lock_is_cleaned_and_a_new_xvfb_started(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The crash-loop repair: a dead owner's lock must stop owning the display."""
    root = _display_env(monkeypatch, injector, tmp_path)
    (root / ".X99-lock").write_text("     4242", encoding="ascii")
    socket_dir = root / ".X11-unix"
    socket_dir.mkdir()
    (socket_dir / "X99").write_bytes(b"stale socket")
    monkeypatch.setattr(injector, "_pid_alive", lambda pid: False)
    up = {"on": False}
    spawns: list[int] = []

    def spawn(display: int) -> None:
        spawns.append(display)
        up["on"] = True

    monkeypatch.setattr(injector, "spawn_xvfb", spawn)
    monkeypatch.setattr(injector, "probe_display", lambda path: up["on"])

    assert injector.ensure_x_display(wait=1.0, poll=0.01) == "repaired"
    assert spawns == [99]
    assert not (root / ".X99-lock").exists(), "the stale lock is what keeps the display dark"
    assert not (socket_dir / "X99").exists()


def test_a_live_owner_is_waited_for_not_stomped(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A lock held by a live PID is believed — removing it would race a real Xvfb."""
    root = _display_env(monkeypatch, injector, tmp_path)
    (root / ".X99-lock").write_text("     4242", encoding="ascii")
    monkeypatch.setattr(injector, "_pid_alive", lambda pid: True)
    answers = [False, True]  # the starting server answers during the wait
    monkeypatch.setattr(injector, "probe_display", lambda path: answers.pop(0) if answers else True)
    spawns: list[int] = []
    monkeypatch.setattr(injector, "spawn_xvfb", spawns.append)

    assert injector.ensure_x_display(wait=1.0, poll=0.01) == "ok"
    assert spawns == []
    assert (root / ".X99-lock").exists()


def test_a_dark_display_after_a_repair_fails_loudly(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repair that does not light the display is an error, not a slow mystery."""
    _display_env(monkeypatch, injector, tmp_path)
    monkeypatch.setattr(injector, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(injector, "probe_display", lambda path: False)
    monkeypatch.setattr(injector, "spawn_xvfb", lambda display: None)

    with pytest.raises(injector.DisplayError, match="stayed dark"):
        injector.ensure_x_display(wait=0.05, poll=0.01)


def test_a_live_owner_that_never_answers_is_not_stomped(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _display_env(monkeypatch, injector, tmp_path)
    (root / ".X99-lock").write_text("     4242", encoding="ascii")
    monkeypatch.setattr(injector, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(injector, "probe_display", lambda path: False)
    spawns: list[int] = []
    monkeypatch.setattr(injector, "spawn_xvfb", spawns.append)

    with pytest.raises(injector.DisplayError, match="lock"):
        injector.ensure_x_display(wait=0.05, poll=0.01)
    assert spawns == []
    assert (root / ".X99-lock").exists()


def test_without_a_local_display_there_is_nothing_to_repair(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No ``$DISPLAY`` — or a remote one, which is somebody else's X server."""
    monkeypatch.setattr(injector, "X_RUNTIME_DIR", str(tmp_path))
    spawns: list[int] = []
    monkeypatch.setattr(injector, "spawn_xvfb", spawns.append)

    assert injector.ensure_x_display() == "skipped"
    monkeypatch.setenv("DISPLAY", "host.example:10.0")
    assert injector.ensure_x_display() == "skipped"
    assert spawns == []


def test_xvfb_is_started_the_way_the_image_starts_it(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The repair replaces the image's own Xvfb line — same geometry, same flags."""
    monkeypatch.setattr(injector, "X_RUNTIME_DIR", str(tmp_path))
    commands: list[list[str]] = []

    class Process:
        pass

    def popen(command: list[str], **kwargs: Any) -> Process:
        commands.append(command)
        return Process()

    monkeypatch.setattr(injector.subprocess, "Popen", popen)
    monkeypatch.setenv("XVFB_WHD", "1920x1080x24")

    injector.spawn_xvfb(99)

    monkeypatch.delenv("XVFB_WHD", raising=False)
    injector.spawn_xvfb(98)

    assert commands == [
        ["Xvfb", ":99", "-ac", "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
        ["Xvfb", ":98", "-ac", "-screen", "0", "1280x720x16", "-nolisten", "tcp"],
    ]


def test_a_live_pid_is_alive(injector: ModuleType) -> None:
    assert injector._pid_alive(os.getpid()) is True


def test_a_dead_pid_is_reported_dead(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel is asked with ``os.kill(pid, 0)`` — posix only, because on
    Windows that call would *terminate* the process instead of asking it."""
    monkeypatch.setattr(injector, "IS_POSIX", True)

    def kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(injector.os, "kill", kill)

    assert injector._pid_alive(4242) is False


# ---------------------------------------------------------------------------
# Root-safe defaults on every launch
# ---------------------------------------------------------------------------


async def test_a_launch_is_hardened_for_a_root_container(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """``no_sandbox=True`` (the error text's advice) is nodriver's *old* name —
    0.32 takes ``sandbox=False``, and that is what lands ``--no-sandbox`` in the
    launch. The two arguments are belt and braces beside it."""
    module = _fake(monkeypatch, injector)
    injector.install(module)

    await module.start(headless=False)

    assert module.calls[0]["sandbox"] is False
    assert module.calls[0]["browser_args"] == [
        "--proxy-server=socks5://127.0.0.1:1080",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
    ]


async def test_an_explicit_sandbox_choice_is_kept(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """Someone who set ``sandbox=`` meant it — this only fills the gap."""
    module = _fake(monkeypatch, injector)
    injector.install(module)

    await module.start(sandbox=True)

    assert module.calls[0]["sandbox"] is True


async def test_the_report_says_which_display_the_launch_had(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """``/doctor`` reads this file; a dark display is a fact it must be able to show."""
    module = _fake(monkeypatch, injector)
    injector.install(module)
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setattr(injector, "probe_display", lambda path: True)

    await module.start(headless=False)

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["display"] == "ok"


# ---------------------------------------------------------------------------
# The display at startup, in every mode
# ---------------------------------------------------------------------------


def test_the_display_is_repaired_even_with_the_proxy_off(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch, report: Path
) -> None:
    """``never`` turns off routing, not the X server — a dark display kills the
    browser either way, so the repair runs before the mode is even read."""
    monkeypatch.setenv("YT_SESSION_PROXY_MODE", "never")
    monkeypatch.setenv("YT_SESSION_ROUTE_FILE", str(report))
    asked: list[str] = []

    def repair() -> str:
        asked.append("display")
        return "repaired"

    monkeypatch.setattr(injector, "ensure_x_display", repair)

    injector.main()

    assert asked == ["display"]
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["display"] == "repaired"


def test_a_display_that_cannot_be_repaired_stops_startup(
    injector: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better a loud exit than Chromium's misleading sandbox error ten seconds on."""

    def repair() -> str:
        raise injector.DisplayError("display :99 stayed dark")

    monkeypatch.setattr(injector, "ensure_x_display", repair)

    with pytest.raises(SystemExit):
        injector.main()
