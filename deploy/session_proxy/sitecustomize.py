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
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
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
    config = kwargs.get("config") or (args[0] if args else None)
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
        # The decision is two small network questions (a trace and a TCP connect) and
        # this is still a browser launch that will take minutes — but there is no
        # reason to hold the event loop for them.
        decision = await asyncio.to_thread(decide)
        path = _env(_ENV_ROUTE_FILE, DEFAULT_ROUTE_FILE)
        applied = _apply(decision, args, kwargs)
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
            | {"applied": applied},
        )
        return await original(*args, **kwargs)

    setattr(start, "_tunnel_proxy_patched", True)
    module.start = start
    return True


def _startup_report() -> None:
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
    write_report(_env(_ENV_ROUTE_FILE, DEFAULT_ROUTE_FILE), decision.payload() | {"applied": "startup"})


def main() -> None:
    mode = proxy_mode()
    proxy = _env(_ENV_PROXY, DEFAULT_PROXY)
    if mode == "never" or not proxy:
        _startup_report()
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
