"""What the bot does with the tunnel at startup — the decision, not the probe.

The probe itself is ``tests/test_proxy_health.py``. This module is about the wiring that
turns its answer into behaviour: the address yt-dlp is actually given (translated for
whoever is asking), the boot line an operator reads, and the one message the admins get
when the bot starts *without* the route it was configured to use.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import main
from core.config import Settings, in_container
from services.proxy_health import TunnelHealth


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _capture_probe(monkeypatch: pytest.MonkeyPatch, health: TunnelHealth) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def fake(
        url: str, *, attempts: int = 5, delay_s: float = 2.0, timeout: float = 8.0
    ) -> TunnelHealth:
        calls.append({"url": url, "attempts": attempts, "delay_s": delay_s})
        # Like the real probe, which reports the address it *asked*, not the setting.
        return replace(health, url=url) if health.configured else health

    monkeypatch.setattr(main.proxy_health, "probe_with_retries", fake)
    return calls


async def test_no_tunnel_configured_is_not_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, YTDLP_PROXY="")
    calls = _capture_probe(monkeypatch, TunnelHealth())

    tunnel = await main.resolve_tunnel(settings)

    assert calls == [], "there is nothing to ask, and a boot should not wait to find out"
    assert not tunnel.configured
    assert main.proxy_health.for_ytdlp(tunnel) == ""


async def test_the_probe_uses_the_address_this_process_can_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`warp:1080` inside the network, the published loopback port outside it.

    One setting, two addresses — the same translation every other helper gets. Without
    it a host run would probe a name that does not resolve and report a healthy tunnel
    as down.
    """
    settings = _settings(monkeypatch, YTDLP_PROXY="http://warp:1080")
    calls = _capture_probe(
        monkeypatch,
        TunnelHealth(url="unused", reachable=True, traced=True, warp="on"),
    )

    tunnel = await main.resolve_tunnel(settings)

    expected = "http://warp:1080" if in_container() else "http://127.0.0.1:1080"
    assert calls == [{"url": expected, "attempts": 6, "delay_s": 2.0}]
    assert main.proxy_health.for_ytdlp(tunnel) == expected, (
        "yt-dlp has to be given the same address the probe just used"
    )


async def test_the_attempt_count_comes_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch, YTDLP_PROXY="http://warp:1080", TUNNEL_PROBE_ATTEMPTS="3", TUNNEL_PROBE_DELAY_S="0.5"
    )
    calls = _capture_probe(monkeypatch, TunnelHealth(url="http://warp:1080", reachable=False))

    await main.resolve_tunnel(settings)

    assert calls[0]["attempts"] == 3 and calls[0]["delay_s"] == 0.5


async def test_a_tunnel_that_never_answers_leaves_the_engine_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the boot probe: a dead proxy must not become *every* failure."""
    settings = _settings(monkeypatch, YTDLP_PROXY="http://warp:1080")
    _capture_probe(
        monkeypatch, TunnelHealth(url="http://warp:1080", reachable=False, detail="refused")
    )

    tunnel = await main.resolve_tunnel(settings)

    assert main.proxy_health.for_ytdlp(tunnel) == ""
    notice = main.proxy_health.startup_notice(tunnel)
    assert notice is not None and "docker compose up -d warp" in notice


async def test_admin_notices_are_best_effort() -> None:
    """One admin who blocked the bot must not take the notice — or the boot — down."""
    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id: int, text: str) -> None:
            if chat_id == 1:
                raise RuntimeError("bot was blocked by the user")
            sent.append((chat_id, text))

    await main.notify_admins(FakeBot(), [1, 2, 3], "notice")  # type: ignore[arg-type]

    assert [chat for chat, _ in sent] == [2, 3]
