"""Put the session generator's Chromium on the tunnel, and prove which route it took.

Why a file with this name exists at all
---------------------------------------
``yt-session-generator`` mints YouTube's ``po_token`` with a *real browser*, and the
browser is the one process in this stack that cannot be pointed at a proxy by
configuration:

* the generator has no proxy setting — measured from its source, it calls
  ``nodriver.start(headless=False, browser_executable_path=..., user_data_dir=...)``
  and nodriver 0.32 exposes ``browser_args`` and nothing proxy-shaped;
* Chromium ignores ``HTTP_PROXY``/``HTTPS_PROXY`` (and ``CHROMIUM_FLAGS`` is read by
  the *launcher script* of some distributions, which nodriver does not use).

So the argument has to reach the actual ``nodriver.start`` call, and the only hook an
image gives us without rebuilding it is Python's own: an interpreter imports
``sitecustomize`` at startup (``site.py``), found through ``PYTHONPATH``. This file is
mounted read-only as ``/opt/session-proxy/sitecustomize.py`` and that directory is
put on ``PYTHONPATH``, so it runs before the generator's first line and can wrap
``nodriver.start`` — the smallest possible intervention on somebody else's image.

What it decides, and why it is measured
---------------------------------------
``network_mode: "service:warp"`` shares the tunnel container's network *namespace*,
which is a stronger guarantee than any proxy variable: the WARP client runs in **warp
mode** by default (its own shipped healthcheck does a *direct* ``curl`` and expects
``warp=on``, which is only possible when the whole namespace is routed), so every
process in that namespace — Chromium included — leaves through WARP with no flag at
all. That is why a proxy flag is not "the fix" on its own, and why this file probes
instead of assuming:

* a *direct* trace that reports ``warp=on`` means packet routing already carries the
  browser — recorded, and reported to the admin;
* the proxy is still forced whenever it answers, because that is the explicit route
  the deployment asked for and it is only *unsafe* when the client is in WARP's
  proxy mode (where direct traffic is not tunnelled and the proxy is the only way
  out). The bot's own tunnel probe reads the proxy's exit address, so the two
  halves together can say whether the browser really is on WARP.

If the proxy is *not* answering, the flag is deliberately left out: a browser pointed
at a dead proxy has no internet at all, whereas the shared namespace may well still be
carrying it. That case is logged loudly and lands in the report file, because a silent
fallback is exactly how "the browser leaks from the host IP" stays invisible.

The decision is taken per browser launch (the generator relaunches every
``--update-interval``, 300s by default), so a WARP client that was switched to proxy
mode later is picked up by the next launch instead of needing a container restart.

Why the display is repaired here too
------------------------------------
The browser needs an X server: the image runs a headed Chromium under Xvfb, and a
Chromium that cannot reach its display exits at once. When the generator's process
dies, the container restarts with the *same* ``/tmp`` — the dead Xvfb's lock file
and socket survive, every later ``Xvfb :99`` refuses the display ("Server is
already active"), and the image's startup script throws that error away and
launches anyway. The failure then surfaces as nodriver's unrelated "pass
no_sandbox=True" guess. This file is imported before the generator's first line,
which makes it the one place that can notice a dark display, clean the stale lock,
and start a fresh Xvfb — before the first browser launch instead of after the
first crash.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

#: Where gost listens inside the WARP image, in both HTTP and SOCKS5. Inside the
#: shared namespace ``127.0.0.1`` *is* the tunnel container, which is why this is not
#: ``warp:1080``: the namespace makes the tunnel local.
DEFAULT_PROXY = "socks5://127.0.0.1:1080"

#: The port a proxy URL without one is assumed to use (gost's port, again).
DEFAULT_PROXY_PORT = 1080

#: Cloudflare's trace endpoint — the same one the bot's tunnel probe reads, so both
#: halves of the answer ("is the namespace tunnelled", "is the proxy tunnelled") are
#: measured against the same source.
DEFAULT_TRACE_URL = "https://cloudflare.com/cdn-cgi/trace"

#: Where the decision is written for `/doctor` to read (a mount shared with the bot).
DEFAULT_ROUTE_FILE = "/runtime/browser-route.json"

#: Bounded on purpose: this runs while the generator starts its browser, and a probe
#: that hangs would delay the one thing the container exists to do.
TRACE_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 2.0

#: The Chromium switch itself. ``socks5://`` (not ``socks5h``) is what Chromium
#: accepts, and it resolves names on the proxy side, which is the point.
PROXY_FLAG = "--proxy-server"

#: Belt and braces beside the sandbox switch below: keep a root-owned Chromium off
#: setuid helpers and off a small ``/dev/shm``. The image raises its shared memory
#: too; this costs nothing and covers the next host the same way.
HARDENING_ARGS = ("--disable-setuid-sandbox", "--disable-dev-shm-usage")

#: Where X11 keeps display sockets and lock files — the directory Xvfb itself
#: manages. A module constant rather than a literal so the repair can be tested
#: against a scratch directory.
X_RUNTIME_DIR = "/tmp"

#: The screen geometry env the image's own startup script gives Xvfb.
_ENV_XVFB_WHD = "XVFB_WHD"
DEFAULT_XVFB_WHD = "1280x720x16"

#: How long a display is given to answer before the repair is called a failure, and
#: how often it is probed. Bounded on purpose: this runs around a browser launch,
#: and an unbounded wait would hide the cause behind the very timeout it prevents.
X_WAIT_S = 15.0
X_POLL_S = 0.25

_ENV_PROXY = "YT_SESSION_CHROMIUM_PROXY"
_ENV_MODE = "YT_SESSION_PROXY_MODE"
_ENV_ROUTE_FILE = "YT_SESSION_ROUTE_FILE"
_ENV_TRACE_URL = "YT_SESSION_TRACE_URL"

#: ``auto`` (force the proxy whenever it answers), ``always`` (force it even if it
#: does not — what to write when the operator wants the argument unconditional),
#: ``never`` (install nothing; the browser uses whatever the namespace does).
_MODES = ("auto", "always", "never")


@dataclass(frozen=True)
class Trace:
    """What the trace endpoint said about the *direct* route from this namespace."""

    ok: bool = False
    warp: str = ""
    exit_ip: str = ""

    @property
    def tunnelled(self) -> bool:
        return self.warp in {"on", "plus"}


@dataclass(frozen=True)
class Decision:
    """The route the browser will take, and the evidence behind it."""

    enabled: bool
    mode: str
    proxy: str = ""
    force: bool = False
    proxy_up: bool = False
    #: ``warp``/``ip`` of a *direct* request from this namespace — the fact that says
    #: whether packet routing alone already carries the browser through the tunnel.
    direct_warp: str = ""
    exit_ip: str = ""
    #: A short code, not a sentence: the report is rendered for admins by the bot, and
    #: a translated message per case belongs there rather than in two languages here.
    reason: str = ""

    @property
    def browser_arg(self) -> str | None:
        return f"{PROXY_FLAG}={self.proxy}" if self.force else None

    def payload(self, *, version: str = "") -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "proxy": self.proxy,
            "force": self.force,
            "proxy_up": self.proxy_up,
            "direct_warp": self.direct_warp,
            "exit_ip": self.exit_ip,
            "reason": self.reason,
            "nodriver": version,
            "updated": int(time.time()),
        }


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def proxy_mode() -> str:
    """The configured mode, with anything unrecognised treated as ``auto``."""
    mode = _env(_ENV_MODE, "auto").lower()
    return mode if mode in _MODES else "auto"


def read_trace(url: str, timeout: float = TRACE_TIMEOUT_S) -> Trace:
    """Ask the trace endpoint, directly, where this namespace's traffic leaves from.

    Never raises: an endpoint that cannot be read means "cannot tell", which is a
    different answer from ``warp=off`` and must not be reported as one.
    """
    if not url:
        return Trace()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — an unreachable probe is an answer
        _log(f"trace probe failed ({type(exc).__name__}: {exc})")
        return Trace()
    values: dict[str, str] = {}
    for line in body.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and value.strip():
            values[key.strip()] = value.strip()
    return Trace(ok=True, warp=values.get("warp", ""), exit_ip=values.get("ip", ""))


def proxy_answers(proxy: str, timeout: float = CONNECT_TIMEOUT_S) -> bool:
    """Is anything listening where the proxy is? A TCP connect, nothing more.

    Deliberately not a SOCKS handshake: the question is only whether pointing
    Chromium there can work at all, and *where that proxy exits* is read by the bot's
    own probe (which speaks HTTP to the same port).
    """
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_PROXY_PORT
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        _log(f"proxy {host}:{port} did not answer ({exc})")
        return False


def decide(*, probe: bool = True) -> Decision:
    """Choose the browser's route from measured facts, never from an assumption."""
    mode = proxy_mode()
    proxy = _env(_ENV_PROXY, DEFAULT_PROXY)
    if mode == "never":
        return Decision(enabled=False, mode=mode, proxy=proxy, reason="disabled")
    if not proxy:
        return Decision(enabled=False, mode=mode, reason="no-proxy-configured")

    direct = read_trace(_env(_ENV_TRACE_URL, DEFAULT_TRACE_URL)) if probe else Trace()
    up = proxy_answers(proxy) if probe else True
    if mode == "always" or up:
        reason = "forced" if mode == "always" else "proxy-up"
        return Decision(
            enabled=True,
            mode=mode,
            proxy=proxy,
            force=True,
            proxy_up=up,
            direct_warp=direct.warp,
            exit_ip=direct.exit_ip,
            reason=reason,
        )
    # The proxy is silent. Sending Chromium there would leave it without a route at
    # all, so the browser keeps the namespace's own pathway — which is *also* not
    # tunnelled when the WARP client is in proxy mode, and the report says so.
    return Decision(
        enabled=True,
        mode=mode,
        proxy=proxy,
        force=False,
        direct_warp=direct.warp,
        exit_ip=direct.exit_ip,
        reason="proxy-down",
    )


def write_report(path: str, payload: dict[str, Any]) -> None:
    """Write the decision for the bot to read. Best effort, and loud when it is not.

    The *fix* (the browser argument) never depends on this file; the file is how the
    finding reaches an admin who is looking at Telegram rather than at logs.
    """
    if not path:
        return
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError as exc:
        _log(f"could not write {path} ({exc}) — the browser route is only in this log")


def _log(message: str) -> None:
    """One line per launch, on stderr — where the generator's own log goes."""
    print(f"[session-proxy] {message}", file=sys.stderr, flush=True)


def _merged_args(arg: str, existing: Any) -> list[str]:
    """The caller's browser arguments, with the proxy added unless they set one."""
    current = [item for item in (existing or []) if isinstance(item, str)]
    if any(item.startswith(f"{PROXY_FLAG}=") for item in current):
        return current
    return [arg, *current]


def _apply(decision: Decision, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Put the proxy into the call, whichever way the caller built it.

    ``nodriver.start`` takes ``browser_args`` as keyword-only, but a ``Config`` object
    (positional or not) is used *instead of* the keyword arguments — writing the
    keyword there would be silently ignored, which is the one failure mode that must
    not happen here.
    """
    arg = decision.browser_arg
    if not arg:
        return "none"
    config = _caller_config(args, kwargs)
    if config is not None:
        if any(
            isinstance(item, str) and item.startswith(f"{PROXY_FLAG}=")
            for item in (getattr(config, "browser_args", None) or [])
        ):
            return "caller"
        added = getattr(config, "add_argument", None)
        if callable(added):
            added(arg)
        else:  # pragma: no cover — a Config-shaped object without add_argument
            setattr(config, "browser_args", [arg])
        return "config"
    if any(
        isinstance(item, str) and item.startswith(f"{PROXY_FLAG}=")
        for item in (kwargs.get("browser_args") or [])
    ):
        return "caller"
    kwargs["browser_args"] = _merged_args(arg, kwargs.get("browser_args"))
    return "kwargs"


def _caller_config(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """The ``Config`` the caller brought, whether positional or keyword."""
    return kwargs.get("config") or (args[0] if args else None)


def _harden(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """Give every launch the root-safe defaults, whichever way the caller built it.

    ``sandbox=False`` is nodriver 0.32's real switch — the ``no_sandbox=True`` the
    connection error suggests is an older name and would be swallowed by
    ``Config(**kwargs)``. It is what makes ``Config.__call__`` append
    ``--no-sandbox``; the two arguments in ``HARDENING_ARGS`` are belt and braces
    beside it. The caller's own values win: this fills gaps, never overrides.
    """
    config = _caller_config(args, kwargs)
    if config is not None:
        if getattr(config, "sandbox", None) is not False:
            setattr(config, "sandbox", False)
        existing = [
            item for item in (getattr(config, "browser_args", None) or []) if isinstance(item, str)
        ]
        missing = [arg for arg in HARDENING_ARGS if arg not in existing]
        added = getattr(config, "add_argument", None)
        if callable(added):
            for arg in missing:
                added(arg)
        elif missing:  # pragma: no cover — a Config-shaped object without add_argument
            setattr(config, "browser_args", [*existing, *missing])
        return
    kwargs.setdefault("sandbox", False)
    current = [item for item in (kwargs.get("browser_args") or []) if isinstance(item, str)]
    kwargs["browser_args"] = [*current, *[arg for arg in HARDENING_ARGS if arg not in current]]


#: Whether signalling a PID can mean "ask" rather than "kill" — on Windows
#: ``os.kill`` terminates the process outright, so it is never called there.
IS_POSIX = os.name == "posix"

#: Unix-domain sockets. Typeshed *and* the Windows runtime omit ``AF_UNIX`` (where
#: the display repair never runs); the attribute exists on every posix Python, and
#: 1 is its value on all of them — a socket built with it fails cleanly elsewhere.
AF_UNIX: int = getattr(socket, "AF_UNIX", 1)


class DisplayError(RuntimeError):
    """``$DISPLAY`` is set but dark, and the repair did not light it."""


def display_number(value: str | None) -> int | None:
    """The number of a *local* display (``:99``, ``:99.0``) — ``None`` otherwise.

    A remote display (``host:10``) is somebody else's X server and is left alone.
    """
    if not value or not value.startswith(":"):
        return None
    number = value[1:].partition(".")[0]
    return int(number) if number.isdigit() else None


def _x_socket_path(display: int) -> str:
    return os.path.join(X_RUNTIME_DIR, ".X11-unix", f"X{display}")


def _x_lock_path(display: int) -> str:
    return os.path.join(X_RUNTIME_DIR, f".X{display}-lock")


def probe_display(path: str, timeout: float = CONNECT_TIMEOUT_S) -> bool:
    """Is an X server answering on this socket? A connect, nothing more."""
    if not os.path.exists(path):
        return False
    try:
        with socket.socket(AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
            return True
    except OSError:
        return False


def _lock_pid(lock_path: str) -> int | None:
    """The PID the lock file claims owns the display — ``None`` when unreadable."""
    try:
        with open(lock_path, encoding="ascii") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a live process.

    The kernel is asked with ``os.kill(pid, 0)``, which only *asks* on posix;
    ``True`` is the conservative answer everywhere else, and a lock that might be
    alive is one this repair must not steal.
    """
    if not IS_POSIX:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def spawn_xvfb(display: int) -> None:
    """Start the X server this display needs — detached, the way the image does."""
    os.makedirs(os.path.dirname(_x_socket_path(display)), exist_ok=True)
    geometry = _env(_ENV_XVFB_WHD, DEFAULT_XVFB_WHD)
    subprocess.Popen(
        ["Xvfb", f":{display}", "-ac", "-screen", "0", geometry, "-nolisten", "tcp"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_x_display(*, wait: float = X_WAIT_S, poll: float = X_POLL_S) -> str:
    """Make sure ``$DISPLAY`` has a live X server, repairing a crashed one.

    The trap: when the generator's process dies, the container restarts with the
    *same* ``/tmp`` — the dead Xvfb's lock file and socket stay behind, every later
    ``Xvfb :99`` refuses the display ("Server is already active"), and the image's
    startup script throws that error away. So a stale lock is cleaned and a fresh
    Xvfb started; a lock held by a *live* PID is believed (another Xvfb is
    mid-start) and only waited on, because removing it would race a real server.

    Returns ``ok`` (already up), ``repaired`` (a fresh Xvfb was started and
    answered), or ``skipped`` (no local ``$DISPLAY`` — nothing of ours to repair).
    Raises :class:`DisplayError` when a display was needed and stayed dark.
    """
    display = display_number(os.environ.get("DISPLAY"))
    if display is None:
        return "skipped"
    socket_path = _x_socket_path(display)
    lock_path = _x_lock_path(display)
    if probe_display(socket_path):
        return "ok"
    deadline = time.monotonic() + wait
    owner = _lock_pid(lock_path)
    if owner is not None and _pid_alive(owner):
        while True:
            if probe_display(socket_path):
                return "ok"
            if time.monotonic() >= deadline:
                raise DisplayError(
                    f"display :{display} stayed dark while pid {owner} held its lock — "
                    "a lock a live process owns is not this repair's to remove"
                )
            time.sleep(poll)
    for stale in (lock_path, socket_path):
        try:
            os.remove(stale)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise DisplayError(f"could not remove stale {stale} ({exc})") from exc
    try:
        spawn_xvfb(display)
    except OSError as exc:
        raise DisplayError(f"Xvfb :{display} could not be started ({exc})") from exc
    while True:
        if probe_display(socket_path):
            return "repaired"
        if time.monotonic() >= deadline:
            raise DisplayError(f"display :{display} stayed dark after {wait:.0f}s of Xvfb")
        time.sleep(poll)


def install(module: Any) -> bool:
    """Wrap ``nodriver.start`` so every launch carries the tunnel decision.

    Returns whether the wrapping happened; the caller (module import) treats ``False``
    as a fatal misconfiguration rather than carrying on unproxied, because a browser
    that quietly leaves from the blocked address is the bug this file exists to fix.
    """
    original = getattr(module, "start", None)
    if original is None:
        return False
    if getattr(original, "_tunnel_proxy_patched", False):
        return True

    async def start(*args: Any, **kwargs: Any) -> Any:
        # The display question is local and comes first: a browser launched against
        # a dark X server fails instantly, in a way that reads like a network fault
        # (and, worse, like the sandbox guess nodriver prints).
        display = await asyncio.to_thread(ensure_x_display)
        if display == "repaired":
            _log(f"display {os.environ.get('DISPLAY', '?')} repaired before launch")
        # The decision is two small network questions (a trace and a TCP connect) and
        # this is still a browser launch that will take minutes — but there is no
        # reason to hold the event loop for them.
        decision = await asyncio.to_thread(decide)
        path = _env(_ENV_ROUTE_FILE, DEFAULT_ROUTE_FILE)
        applied = _apply(decision, args, kwargs)
        _harden(args, kwargs)
        if decision.force:
            _log(
                f"chromium forced through {decision.proxy} "
                f"({decision.reason}; direct route warp={decision.direct_warp or '?'}"
                + (f", exit {decision.exit_ip}" if decision.exit_ip else "")
                + ")"
            )
        else:
            _log(
                f"chromium NOT proxied ({decision.reason}); direct route "
                f"warp={decision.direct_warp or '?'}"
                + (f" from {decision.exit_ip}" if decision.exit_ip else "")
                + " — the browser leaves from whatever this namespace does"
            )
        write_report(
            path,
            decision.payload(version=str(getattr(module, "__version__", "")))
            | {"applied": applied, "display": display},
        )
        return await original(*args, **kwargs)

    setattr(start, "_tunnel_proxy_patched", True)
    module.start = start
    return True


def _startup_report(display: str = "skipped") -> None:
    """Record the *configuration* even when the browser is not being proxied.

    Without this, "no report file" would have two meanings — off, or never launched —
    and the admin's one-line summary could not tell them apart.
    """
    decision = decide(probe=False)
    decision = Decision(
        enabled=decision.enabled,
        mode=decision.mode,
        proxy=decision.proxy,
        reason=decision.reason,
    )
    write_report(
        _env(_ENV_ROUTE_FILE, DEFAULT_ROUTE_FILE),
        decision.payload() | {"applied": "startup", "display": display},
    )


def main() -> None:
    # Before anything else: the display is needed in *every* mode, and a dark one
    # would otherwise surface as nodriver's unrelated sandbox guess ten seconds on.
    try:
        display = ensure_x_display()
    except DisplayError as exc:
        _log(f"FATAL: {exc} — the browser cannot start against a dark display")
        raise SystemExit(1) from exc
    if display != "skipped":
        _log(f"display {os.environ.get('DISPLAY', '?')} {display}")
    mode = proxy_mode()
    proxy = _env(_ENV_PROXY, DEFAULT_PROXY)
    if mode == "never" or not proxy:
        _startup_report(display)
        _log(f"not installed (mode={mode}, proxy={proxy or 'empty'})")
        return
    try:
        import nodriver
    except Exception as exc:  # noqa: BLE001 — this container cannot work without it
        _log(f"FATAL: cannot import nodriver ({type(exc).__name__}: {exc})")
        raise SystemExit(1) from exc
    if not install(nodriver):
        _log("FATAL: nodriver.start could not be wrapped — refusing to leave the browser unproxied")
        raise SystemExit(1)
    _log(
        f"installed (mode={mode}, proxy={proxy}) — every Chromium launch is decided "
        "at launch time and written to the route file"
    )


# Auto-install only when Python imports this file *as* ``sitecustomize`` (which is
# how the mount is arranged) — or when someone runs it by hand to see the verdict.
# Any other import name is a reader (a test), not the generator's interpreter.
if __name__ in {"sitecustomize", "__main__"}:
    main()
