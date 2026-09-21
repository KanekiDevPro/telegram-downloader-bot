"""The permanent watch over the three helpers: what it records, and when it pages.

``/doctor`` is a human asking a question. This is the machine asking it every few
minutes, and the difference is what these tests are about: a state is written down
only when it *changes*, a page happens only after an outage outlasts a restart, and
the same outage cannot page twice in an hour. All of it over in-memory stand-ins —
the sweep's own probes are stubbed by ``tests/conftest.py``, which is why the module
resolves them through ``services.doctor`` instead of binding them at import time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import pytest

from core.config import Settings
from services import doctor as doctor_service
from services import helper_watch
from services.doctor import PotProvider, SessionServer
from services.helper_watch import (
    ALERT_AFTER_S,
    ALERT_COOLDOWN_S,
    ALERTED_AT_KEY,
    DOWN_SINCE_KEY,
    POT_HELPER,
    SESSION_HELPER,
    TUNNEL_HELPER,
    HelperState,
    Outage,
    WatchResult,
    describe,
    observe,
    render_alert,
    render_recovery,
    render_summary,
    run_helper_watch,
    summary,
    watch,
)
from services.proxy_health import TunnelHealth

POT_URL = "http://pot-provider:4416"
SESSION_URL = "http://yt-session-generator:8080"
TUNNEL_URL = "http://warp:1080"

#: Every query these tests reach goes through ``FakeStore``, which ignores it.
POOL: Any = object()


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _both_wired(monkeypatch: pytest.MonkeyPatch) -> Settings:
    return _settings(
        monkeypatch,
        YTDLP_POT_PROVIDER_URL=POT_URL,
        YOUTUBE_SESSION_SERVER=SESSION_URL,
        YTDLP_PROXY=TUNNEL_URL,
        COOKIE_FILE="",
    )


async def _sweep(
    settings: Settings,
    *,
    bot: Any = None,
    admins: Sequence[int] = (),
    now: float,
) -> WatchResult:
    """``watch`` with the fakes accepted once, instead of an ignore on every call."""
    return await watch(POOL, settings, bot=bot, admin_ids=admins, now=now)


# ---------------------------------------------------------------------------
# Fakes: the bot, and the two tables this module owns
# ---------------------------------------------------------------------------


class FakeBot:
    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failing = set(failing)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id in self.failing:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


class FakeStore:
    """``bot_state`` and ``helper_events`` in memory (the module's whole surface)."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.rows: list[tuple[datetime, str, str, str]] = []
        self.last: dict[str, tuple[str, str]] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def get_state(pool: Any, key: str) -> str | None:
            return self.state.get(key)

        async def set_state(pool: Any, key: str, value: str) -> None:
            self.state[key] = value

        async def record_helper_event(
            pool: Any, *, helper: str, state: str, reason: str = ""
        ) -> None:
            self.rows.append((datetime.now(timezone.utc), helper, state, reason))
            self.last[helper] = (state, reason)

        async def last_helper_event(pool: Any, helper: str) -> tuple[str, str] | None:
            return self.last.get(helper)

        async def helper_events(
            pool: Any, since: datetime, until: datetime | None = None
        ) -> list[tuple[datetime, str, str, str]]:
            return [row for row in self.rows if row[0] >= since]

        monkeypatch.setattr(helper_watch.database, "get_state", get_state)
        monkeypatch.setattr(helper_watch.database, "set_state", set_state)
        monkeypatch.setattr(helper_watch.database, "record_helper_event", record_helper_event)
        monkeypatch.setattr(helper_watch.database, "last_helper_event", last_helper_event)
        monkeypatch.setattr(helper_watch.database, "helper_events", helper_events)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore()
    fake.install(monkeypatch)
    return fake


def _provider_up(monkeypatch: pytest.MonkeyPatch, version: str = "2.0.0") -> None:
    async def probe(url: str, timeout: float = 5.0) -> PotProvider:
        return PotProvider(reachable=True, version=version)

    monkeypatch.setattr(doctor_service, "probe_pot_provider", probe)


def _provider_down(monkeypatch: pytest.MonkeyPatch, error: str = "پاسخ نمی‌دهد") -> None:
    async def probe(url: str, timeout: float = 5.0) -> PotProvider:
        return PotProvider(reachable=False, error=error)

    monkeypatch.setattr(doctor_service, "probe_pot_provider", probe)


def _session(monkeypatch: pytest.MonkeyPatch, server: SessionServer) -> None:
    async def probe(url: str, timeout: float = 5.0) -> SessionServer:
        return server

    monkeypatch.setattr(doctor_service, "probe_session_server", probe)


def _tunnel(monkeypatch: pytest.MonkeyPatch, health: TunnelHealth) -> None:
    async def probe(url: str, timeout: float = 5.0) -> TunnelHealth:
        return health

    monkeypatch.setattr(doctor_service, "probe_tunnel", probe)


def _state(states: tuple[HelperState, ...], helper: str) -> HelperState:
    return next(state for state in states if state.helper == helper)


# ---------------------------------------------------------------------------
# Classifying what a sweep sees
# ---------------------------------------------------------------------------


async def test_a_helper_nobody_configured_is_off_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        YTDLP_POT_PROVIDER_URL="",
        YOUTUBE_SESSION_SERVER="",
        YTDLP_PROXY="",
    )

    states = await observe(settings)

    # Three helpers, and "off" is not "broken": a deployment that never configured
    # a route has nothing to be down, and it must not fill the history with rows.
    assert [state.state for state in states] == ["off", "off", "off"]
    assert all(state.gone for state in states)
    assert not any(state.failing for state in states)


async def test_a_version_mismatch_is_recorded_as_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin *rejects* a server whose major differs: it answers, and is useless."""
    settings = _both_wired(monkeypatch)
    _provider_up(monkeypatch, "3.1.0")
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    monkeypatch.setattr(helper_watch, "pot_plugin_version", lambda: "2.0.0")

    state = _state(await observe(settings), POT_HELPER)

    assert state.state == "drift"
    assert "v3.1.0" in state.reason and "v2.0.0" in state.reason


async def test_a_server_still_making_its_first_token_is_warming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_up(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=False, error="توکن هنوز ساخته نشده"))

    state = _state(await observe(settings), SESSION_HELPER)

    assert state.state == "warming"
    assert not state.failing
    assert not state.gone, "warming is a working route, not a missing one"


async def test_a_token_that_came_back_short_is_said_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_up(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=100))

    state = _state(await observe(settings), SESSION_HELPER)

    assert state.state == "ok"
    assert "100" in state.reason and "کوتاه" in state.reason


async def test_a_tunnel_that_answers_without_warp_is_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case the tunnel exists for: it is up, and it is still the flagged IP.

    A watch that read "port open" as 🟢 would report a healthy tunnel on exactly the
    deployment whose downloads are blocked — which is why ``drift`` is a *failing*
    state here, like the plugin/server version mismatch above.
    """
    settings = _both_wired(monkeypatch)
    _provider_up(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    _tunnel(
        monkeypatch,
        TunnelHealth(url=TUNNEL_URL, reachable=True, traced=True, warp="off"),
    )

    state = _state(await observe(settings), TUNNEL_HELPER)

    assert state.state == "drift"
    assert state.failing
    assert "warp=off" in state.reason, "the reason is what makes the page actionable"


async def test_a_tunnel_that_does_not_answer_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_up(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    _tunnel(
        monkeypatch,
        TunnelHealth(url=TUNNEL_URL, reachable=False, detail="ConnectionRefusedError"),
    )

    state = _state(await observe(settings), TUNNEL_HELPER)

    assert state.state == "down"
    assert "ConnectionRefusedError" in state.reason
    assert TUNNEL_HELPER in [state.helper for state in (await observe(settings))], (
        "and the watcher has something to page about"
    )


# ---------------------------------------------------------------------------
# The history: a row per change, not per sweep
# ---------------------------------------------------------------------------


async def test_the_same_state_twice_is_one_row(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_up(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))

    first = await _sweep(settings, now=1000.0)
    second = await _sweep(settings, now=1300.0)

    assert len(first.transitions) == 3 and len(second.transitions) == 0
    assert len(store.rows) == 3, "a healthy helper is one row in the history"


async def test_a_failure_is_recorded_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_down(monkeypatch, "HTTP 500")
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))

    await _sweep(settings, now=1000.0)

    _, helper, state, reason = store.rows[0]
    assert (helper, state, reason) == (POT_HELPER, "down", "HTTP 500")


async def test_a_diagnostic_run_records_without_paging(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    """No bot means no chat: the history is the part that must not depend on one."""
    settings = _both_wired(monkeypatch)
    _provider_down(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))

    result = await _sweep(settings, now=1000.0)

    assert store.rows and result.alerted == ()
    assert DOWN_SINCE_KEY.format(helper=POT_HELPER) in store.state


# ---------------------------------------------------------------------------
# The page: after a restart-sized wait, once per outage, and when it ends
# ---------------------------------------------------------------------------


async def test_a_restart_sized_outage_does_not_page(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_down(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    bot = FakeBot()

    first = await _sweep(settings, bot=bot, admins=[1], now=1000.0)
    again = await _sweep(settings, bot=bot, admins=[1], now=1000.0 + ALERT_AFTER_S - 1)

    assert first.alerted == () and again.alerted == ()
    assert bot.sent == [], "a container being replaced is not an incident"


async def test_a_long_outage_pages_once_per_cooldown(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_down(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    bot = FakeBot()
    start = 10_000.0

    await _sweep(settings, bot=bot, admins=[1], now=start)
    paged = await _sweep(settings, bot=bot, admins=[1], now=start + ALERT_AFTER_S)
    soon = await _sweep(settings, bot=bot, admins=[1], now=start + ALERT_AFTER_S + 60)
    later = await _sweep(
        settings, bot=bot, admins=[1], now=start + ALERT_AFTER_S + ALERT_COOLDOWN_S
    )

    assert len(paged.alerted) == 1 and soon.alerted == () and len(later.alerted) == 1
    assert len(bot.sent) == 2
    assert ALERTED_AT_KEY.format(helper=POT_HELPER) in store.state


async def test_recovery_is_announced_and_resets_the_clock(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    bot = FakeBot()
    start = 20_000.0
    _provider_down(monkeypatch)
    await _sweep(settings, bot=bot, admins=[1], now=start)
    await _sweep(settings, bot=bot, admins=[1], now=start + ALERT_AFTER_S)
    _provider_up(monkeypatch)

    back = await _sweep(settings, bot=bot, admins=[1], now=start + ALERT_AFTER_S + 60)

    assert [state.helper for state in back.recovered] == [POT_HELPER]
    assert "برگشت" in bot.sent[-1][1]
    assert store.state[DOWN_SINCE_KEY.format(helper=POT_HELPER)] == "", (
        "a recovered helper starts the next outage from zero, so it cannot inherit an "
        "old clock and page on the first sighting"
    )


async def test_drift_pages_because_a_route_that_answers_and_is_refused_is_dead(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    _provider_up(monkeypatch, "3.1.0")
    monkeypatch.setattr(helper_watch, "pot_plugin_version", lambda: "2.0.0")
    bot = FakeBot()

    drift = await _sweep(settings, bot=bot, admins=[1], now=1000.0)
    paged = await _sweep(settings, bot=bot, admins=[1], now=1000.0 + ALERT_AFTER_S)

    assert _state(drift.states, POT_HELPER).state == "drift"
    assert len(paged.alerted) == 1
    assert "pot-provider" in bot.sent[-1][1]


async def test_one_unreachable_admin_does_not_stop_the_other(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    _provider_down(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    bot = FakeBot(failing=(1,))

    await _sweep(settings, bot=bot, admins=[1, 2], now=1000.0)
    await _sweep(settings, bot=bot, admins=[1, 2], now=1000.0 + ALERT_AFTER_S)

    assert [chat for chat, _ in bot.sent] == [2]


# ---------------------------------------------------------------------------
# The report: how long, and what it cost
# ---------------------------------------------------------------------------


def test_the_alert_names_the_price_and_the_command() -> None:
    text = render_alert((HelperState(SESSION_HELPER, "down", "پاسخ نمی‌دهد"),))

    assert "سرور سشن یوتیوب" in text
    assert "پاسخ نمی‌دهد" in text
    assert "موتور جایگزین" in text, "what the failure costs the users"
    assert "docker compose up -d yt-session-generator" in text
    assert "بقیهٔ ربات" in text, "a helper is one route, not the bot"


def test_the_recovery_message_is_a_plain_answer() -> None:
    text = render_recovery((HelperState(POT_HELPER, "ok", "v2.0.0"),))

    assert "برگشت" in text and "v2.0.0" in text


async def test_the_summary_counts_an_outage_that_is_still_open(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    store.rows = [
        (now - timedelta(hours=5), POT_HELPER, "ok", ""),
        (now - timedelta(hours=3), POT_HELPER, "down", "پاسخ نمی‌دهد"),
    ]

    outages = await summary(POOL, days=7, now=now)

    assert [outage.helper for outage in outages] == [POT_HELPER]
    assert outages[0].hours == 3.0
    assert outages[0].transitions == 2
    assert outages[0].last_reason == "پاسخ نمی‌دهد"


async def test_the_summary_stops_counting_at_recovery(store: FakeStore) -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    store.rows = [
        (now - timedelta(hours=6), POT_HELPER, "down", ""),
        (now - timedelta(hours=2), POT_HELPER, "ok", "v2.0.0"),
    ]

    outages = await summary(POOL, days=7, now=now)

    assert outages[0].hours == 4.0
    assert outages[0].last_reason == "", "the reason belongs to the down row"


async def test_a_quiet_window_says_so(store: FakeStore) -> None:
    assert await summary(POOL, days=7) == []
    assert "قطعی" in render_summary([])


def test_the_summary_sorts_the_worst_between_first() -> None:
    text = render_summary(
        [
            Outage(helper=POT_HELPER, hours=9.5, transitions=4, last_reason="HTTP 500"),
            Outage(helper=SESSION_HELPER, hours=1.0, transitions=2),
        ]
    )

    assert text.index("provider توکن") < text.index("سرور سشن")
    assert "9.5" in text and "HTTP 500" in text


def test_describe_uses_the_same_vocabulary_as_the_report() -> None:
    line = describe((HelperState(POT_HELPER, "ok"), HelperState(SESSION_HELPER, "down")))

    assert line == "provider توکن (PO token): ok | سرور سشن یوتیوب: down"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def test_the_loop_sweeps_then_stops_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    settings.helper_watch_interval_s = 0.01
    _provider_up(monkeypatch)
    _session(monkeypatch, SessionServer(True, ready=True, token_length=200))
    _tunnel(monkeypatch, TunnelHealth(url=TUNNEL_URL, reachable=True, traced=True, warp="on"))
    stop = asyncio.Event()

    task = asyncio.create_task(run_helper_watch(stop, POOL, settings=settings))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert store.rows, "a sweep ran and wrote its first sighting"
    assert len(store.rows) == 3, "and only the *change* is a row, however many sweeps ran"


async def test_the_loop_costs_nothing_when_it_is_off(
    monkeypatch: pytest.MonkeyPatch, store: FakeStore
) -> None:
    settings = _both_wired(monkeypatch)
    settings.helper_watch_interval_s = 0

    await run_helper_watch(asyncio.Event(), POOL, settings=settings)

    assert store.rows == []
