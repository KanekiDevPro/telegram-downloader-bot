"""The yt-dlp tunnel: what the probe can say, and what the bot does with it.

``YTDLP_PROXY`` is the one setting in this deployment that can take *every* download
down at once, so the module has two jobs, and neither is a formality: answer "is this
route usable?" (and, when it is, "is it actually a *different* address?"), and turn the
answer into the decision the bot makes — hand it to yt-dlp, or run direct with one
warning and one page. The split between ``reachable`` and ``traced`` is the part worth
pinning: a SOCKS5 proxy answers and cannot be asked where it goes, and calling that
"warp is off" would page an admin for a correctly configured deployment.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from services import proxy_health

TRACE = """fl=123abc
h=cloudflare.com
ip=198.51.100.7
warp=plus
ts=1700000000.0
"""


def _tunnel(**overrides: Any) -> proxy_health.TunnelHealth:
    base: dict[str, Any] = {
        "url": "http://warp:1080",
        "reachable": True,
        "traced": True,
        "warp": "on",
        "exit_ip": "198.51.100.7",
        "detail": "HTTP 200",
    }
    base.update(overrides)
    return proxy_health.TunnelHealth(**base)


# ---------------------------------------------------------------------------
# What the probe reads
# ---------------------------------------------------------------------------


async def test_the_trace_is_read_through_the_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    async def fake_trace(proxy: str | None, timeout: float) -> tuple[int, str]:
        seen.append(proxy)
        return 200, TRACE

    monkeypatch.setattr(proxy_health, "_trace", fake_trace)

    health = await proxy_health.probe("http://warp:1080")

    assert seen == ["http://warp:1080"], "the probe must go *through* the proxy"
    assert health.reachable and health.traced
    assert health.warp == "plus" and health.exit_ip == "198.51.100.7"
    assert health.on_warp and health.usable and health.state == "ok"


async def test_a_url_without_a_scheme_gets_one(tmp_path: Any) -> None:
    """`warp:1080` is what people write; the trace call needs a scheme."""
    assert proxy_health._as_url("warp:1080") == "http://warp:1080"
    assert proxy_health._as_url("http://warp:1080") == "http://warp:1080"


async def test_an_empty_setting_is_not_a_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_trace(proxy: str | None, timeout: float) -> tuple[int, str]:
        nonlocal attempts
        attempts += 1
        return 200, TRACE

    monkeypatch.setattr(proxy_health, "_trace", fake_trace)

    health = await proxy_health.probe("")
    retried = await proxy_health.probe_with_retries("")

    assert not health.configured and not health.reachable
    assert health.state == "off"
    assert not retried.configured
    assert attempts == 0, "an unset proxy is not a tunnel to ask about"


async def test_a_proxy_that_refuses_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(proxy: str | None, timeout: float) -> tuple[int, str]:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(proxy_health, "_trace", refuse)

    health = await proxy_health.probe("http://warp:1080")

    assert not health.reachable and health.state == "down"
    assert "ConnectionRefusedError" in health.detail


async def test_a_socks5_tunnel_is_reachable_but_not_traced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiohttp cannot dial SOCKS, so the honest answer is "listening, unknown exit".

    This is the case the ``traced`` flag exists for: the deployment did exactly what it
    was told, so it must not be reported as a tunnel that answers from the wrong
    address — that is ``drift``, and ``drift`` pages.
    """

    async def accepts(host: str, port: int) -> tuple[Any, Any]:
        return object(), _Closer()

    monkeypatch.setattr(asyncio, "open_connection", accepts)

    health = await proxy_health.probe("socks5://warp:1080")

    assert health.reachable and not health.traced
    assert health.state == "ok", "reachable without an exit address is not drift"
    assert not health.wrong_exit, "…and nothing is claimed about where it goes"
    assert health.warp == "", "there is no warp value to report"
    assert proxy_health.for_ytdlp(health) == "socks5://warp:1080"


async def test_a_tunnel_that_answers_without_warp_is_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_trace(proxy: str | None, timeout: float) -> tuple[int, str]:
        return 200, "ip=203.0.113.5\nwarp=off\n"

    monkeypatch.setattr(proxy_health, "_trace", fake_trace)

    health = await proxy_health.probe("http://warp:1080")

    assert health.state == "drift"
    assert health.wrong_exit, "the tunnel answers and leaves from the host anyway"
    assert "warp=off" in health.line()


async def test_the_retry_loop_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """WARP registers seconds after it starts — a few tries, then the decision."""
    attempts = 0

    async def refuse(proxy: str | None, timeout: float) -> tuple[int, str]:
        nonlocal attempts
        attempts += 1
        raise OSError("not up yet")

    monkeypatch.setattr(proxy_health, "_trace", refuse)

    health = await proxy_health.probe_with_retries(
        "http://warp:1080", attempts=3, delay_s=0
    )

    assert attempts == 3, "bounded: the boot waits for this"
    assert not health.reachable


async def test_a_tunnel_that_comes_up_after_a_retry_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    async def flaky(proxy: str | None, timeout: float) -> tuple[int, str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("still registering")
        return 200, TRACE

    monkeypatch.setattr(proxy_health, "_trace", flaky)

    health = await proxy_health.probe_with_retries(
        "http://warp:1080", attempts=5, delay_s=0
    )

    assert attempts == 2 and health.on_warp


async def test_the_hosts_own_address_is_asked_without_a_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str | None] = []

    async def fake_trace(proxy: str | None, timeout: float) -> tuple[int, str]:
        seen.append(proxy)
        return 200, TRACE

    monkeypatch.setattr(proxy_health, "_trace", fake_trace)

    assert await proxy_health.direct_exit_ip() == "198.51.100.7"
    assert seen == [None], "the comparison is worthless through the tunnel itself"


async def test_an_unreadable_host_address_is_empty_not_the_tunnels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Cannot tell" must never look like "the same address"."""

    async def refuse(proxy: str | None, timeout: float) -> tuple[int, str]:
        raise OSError("no outbound access")

    monkeypatch.setattr(proxy_health, "_trace", refuse)

    assert await proxy_health.direct_exit_ip() == ""


# ---------------------------------------------------------------------------
# What the bot does with the answer
# ---------------------------------------------------------------------------


def test_a_proxy_that_is_down_is_not_handed_to_the_engine() -> None:
    """A dead proxy is worse than none: it would fail *every* link."""
    down = _tunnel(reachable=False, traced=False)
    untraced = _tunnel(traced=False, warp="")
    off_warp = _tunnel(warp="off")

    assert proxy_health.for_ytdlp(down) == ""
    assert proxy_health.for_ytdlp(_tunnel()) == "http://warp:1080"
    assert proxy_health.for_ytdlp(untraced) == "http://warp:1080"
    assert (
        proxy_health.for_ytdlp(off_warp) == "http://warp:1080"
    ), "warp=off is no worse than direct, and dropping it hides the setting"
    assert proxy_health.for_ytdlp(proxy_health.TunnelHealth()) == ""


def test_the_boot_line_names_the_address() -> None:
    assert "none" in proxy_health.boot_line(proxy_health.TunnelHealth())
    assert "198.51.100.7" in proxy_health.boot_line(_tunnel())
    assert "DOWN" in proxy_health.boot_line(_tunnel(reachable=False, detail="refused"))
    assert "refused" in proxy_health.boot_line(_tunnel(reachable=False, detail="refused"))


def test_only_a_configured_and_unusable_tunnel_pages_at_boot() -> None:
    """Once per boot, and only when the bot really did start without the route."""
    assert proxy_health.startup_notice(proxy_health.TunnelHealth()) is None
    assert proxy_health.startup_notice(_tunnel()) is None
    assert (
        proxy_health.startup_notice(_tunnel(traced=False, warp="")) is None
    ), "a SOCKS5 tunnel is asked once and cannot answer — that is not a fault"

    down = proxy_health.startup_notice(_tunnel(reachable=False, detail="refused"))
    assert down is not None
    assert "docker compose up -d warp" in down and "refused" in down

    off_warp = proxy_health.startup_notice(_tunnel(warp="off"))
    assert off_warp is not None
    assert "WARP" in off_warp and "logs warp" in off_warp

    assert "دانلودها" in (down or "") and down != off_warp, (
        "down and not-on-WARP are different problems with different fixes"
    )


class _Closer:
    """The smallest thing ``_tcp_probe`` can close."""

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass
