"""A permanent watch over the helper servers, and the report it feeds.

The helpers are the three things this stack runs *for* the extractors: the PO-token
provider and session server that YouTube routes need, and the WARP tunnel the primary
engine leaves from. Each is optional and each has one failure mode worth paging on.

``/doctor`` answers "what is wrong right now", asked by a human who already suspects
something. This module answers the other half of the question: *when* did a helper
break, for how long, and does anyone have to know? A provider that dies at 3am is
invisible until a user's YouTube link fails — and by then the only evidence left is
that the link failed, which looks exactly like a blocked IP.

What it does, once per ``HELPER_WATCH_INTERVAL_S``:

* asks every helper the same cheap questions ``/doctor`` does (local traffic, no
  extraction),
* writes a row **only when a state changes**, with the reason — so the table is a
  history, not a log,
* pages the admins once when a helper has been down long enough to be a real outage
  rather than a restart, and once again when it comes back,
* and gives the weekly digest one paragraph: which helper was down, for how many
  hours, and what it cost the users while it was.

Nothing here can break a download: every write is best-effort, and a helper that is
down is a *degraded* route, not a dead bot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import asyncpg
from aiogram import Bot

from core import database
from core.config import Settings, get_settings, probe_url
from core.utils import utcnow
from services import doctor
from services.doctor import PotProvider, SessionServer
from services.extractor import pot_plugin_version
from services.proxy_health import TunnelHealth

logger = logging.getLogger(__name__)#: The helper as it is stored in ``helper_events`` and keyed in messages.
POT_HELPER = "pot"
SESSION_HELPER = "session"
#: Not an HTTP helper like the other two, but watched the same way for the same
#: reason: the tunnel is what the *primary* extractor sends every byte through, so a
#: tunnel that dies is the one helper whose failure changes how every other failure
#: is explained (see ``services/proxy_health.py``).
TUNNEL_HELPER = "tunnel"

HELPER_LABELS: dict[str, str] = {
    POT_HELPER: "provider توکن (PO token)",
    SESSION_HELPER: "سرور سشن یوتیوب",
    TUNNEL_HELPER: "تونل (WARP)",
}

#: What a helper being down costs a user — the first question an admin asks, and the
#: one a bare "❌ down" does not answer.
HELPER_LOSS: dict[str, str] = {
    POT_HELPER: "دانلودها بدون توکن ادامه پیدا میکنند، پس یوتیوب روی IP فلگشده سختتر رد میکند",
    SESSION_HELPER: "موتور جایگزین یوتیوب را بدون سشن میبیند، پس لینکهای بلاکشده از آن هم رد نمیشوند",
    TUNNEL_HELPER: (
        "دانلودها دیگر از IP اشتراکی WARP بیرون نمیروند؛ اگر IP این هاست فلگ شده باشد، "
        "یوتیوب همان بلاک قبلی را برمیگرداند"
    ),
}


HELPER_FIXES: dict[str, str] = {
    POT_HELPER: "docker compose up -d pot-provider",
    SESSION_HELPER: (
        "docker compose up -d yt-session-generator — و اگر بالا بود، لاگش میگوید "
        "چرا توکن نمیسازد (docker compose logs yt-session-generator)"
    ),
    TUNNEL_HELPER: (
        "docker compose up -d warp — و اگر بالا بود، `docker compose logs warp`؛ "
        "WARP چند ثانیه بعد از استارت ثبت میشود، پس یک بار ریاستارت هم امتحان کنید"
    ),
}

#: How long a helper has to stay down before the admins are paged. Below this, a
#: `docker compose restart` or a container being replaced is not an incident — and a
#: page per restart is how admin alerts get muted for good.
ALERT_AFTER_S = 600.0
#: One page per helper per this window, so a flapping helper cannot flood the chat.
ALERT_COOLDOWN_S = 3600.0
#: Where the streak and the last page live (``bot_state``, so they survive a restart).
DOWN_SINCE_KEY = "helper_down_since:{helper}"
ALERTED_AT_KEY = "helper_alerted_at:{helper}"


@dataclass(frozen=True)
class HelperState:
    """What one helper looks like right now, in the doctor's own vocabulary."""

    helper: str
    state: str  # off | ok | warming | drift | down
    reason: str = ""

    @property
    def label(self) -> str:
        return HELPER_LABELS.get(self.helper, self.helper)

    @property
    def failing(self) -> bool:
        """Whether the route is dead — which drift is, silently.

        A server whose major version the plugin refuses answers every health check
        with a 🟢, and turns every token into a rejection. Treating that as healthy
        would make this watch exactly as blind as reading logs.
        """
        return self.state in {"down", "drift"}

    @property
    def gone(self) -> bool:
        """Whether the route is *unusable* — which includes "not configured"."""
        return self.state in {"down", "off"}


@dataclass(frozen=True)
class Outage:
    """One helper's downtime inside a window (the digest's unit)."""

    helper: str
    hours: float
    transitions: int
    last_reason: str = ""

    @property
    def label(self) -> str:
        return HELPER_LABELS.get(self.helper, self.helper)


@dataclass(frozen=True)
class WatchResult:
    """What one sweep saw, wrote, and said."""

    states: tuple[HelperState, ...]
    transitions: tuple[HelperState, ...]
    alerted: tuple[HelperState, ...]
    recovered: tuple[HelperState, ...]


# ---------------------------------------------------------------------------
# Asking (the cheap half of /doctor, without a human)
# ---------------------------------------------------------------------------


async def observe(settings: Settings) -> tuple[HelperState, ...]:
    """Probe both helpers and classify them. Local traffic, never extraction."""
    states: list[HelperState] = []

    if not settings.wants_pot_provider:
        states.append(HelperState(POT_HELPER, "off"))
    else:
        # Through the ``doctor`` module (not a bound name) so every test that stubs
        # the probes — ``tests/conftest.py`` does it for the whole suite — stubs
        # this sweep too, and no test ever talks to a developer's running stack.
        provider: PotProvider = await doctor.probe_pot_provider(
            settings.ytdlp_pot_provider_url
        )
        if not provider.reachable:
            states.append(HelperState(POT_HELPER, "down", provider.error or "پاسخ نمیدهد"))
        elif provider.error:
            states.append(HelperState(POT_HELPER, "down", provider.error))
        else:
            plugin = pot_plugin_version()
            if plugin and provider.version and _major(provider.version) != _major(plugin):
                # Answers perfectly and is still useless: the plugin rejects it.
                states.append(
                    HelperState(
                        POT_HELPER,
                        "drift",
                        f"نسخهٔ سرور v{provider.version} با پلاگین v{plugin} ناهماهمانگ",
                    )
                )
            else:
                states.append(HelperState(POT_HELPER, "ok", provider.version))

    if not settings.ytdlp_proxy.strip():
        # Nothing to watch: without a proxy there is no route to be down, and a
        # permanent 🟢 for a setting nobody wrote is noise in the history.
        states.append(HelperState(TUNNEL_HELPER, "off"))
    else:
        # Translated like every other helper address: the sweep runs in the same
        # process as the bot, so it has to ask the tunnel the bot actually uses.
        tunnel: TunnelHealth = await doctor.probe_tunnel(probe_url(settings.ytdlp_proxy))
        # ``TunnelHealth.state`` already speaks this vocabulary (off | ok | drift |
        # down), and its ``drift`` is the honest one: a proxy that answers but is not
        # on WARP is not broken — it is the same flagged address, which is a different
        # fix, and a watch that called it 🟢 would hide exactly the case it exists for.
        states.append(HelperState(TUNNEL_HELPER, tunnel.state, tunnel.describe()))

    if not settings.wants_session_server:
        states.append(HelperState(SESSION_HELPER, "off"))
    else:
        server: SessionServer = await doctor.probe_session_server(
            settings.youtube_session_server
        )
        if not server.reachable:
            states.append(HelperState(SESSION_HELPER, "down", server.error or "پاسخ نمیدهد"))
        elif not server.ready:
            # Alive and working on its first token is *not* an outage.
            states.append(HelperState(SESSION_HELPER, "warming", server.error))
        else:
            states.append(
                HelperState(
                    SESSION_HELPER,
                    "ok",
                    f"توکن {server.token_length} کاراکتری"
                    + ("" if not server.short else " (کوتاه — کوبالت هشدار میدهد)"),
                )
            )

    return tuple(states)


def _major(version: str) -> str:
    return version.split(".", 1)[0]


# ---------------------------------------------------------------------------
# Writing it down, and telling someone
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return f"{time.time():.3f}"


def _as_stamp(value: str | None) -> float | None:
    """Read a stored stamp; empty or unreadable means "no outage is being tracked"."""
    try:
        return float(value) if value else None
    except ValueError:
        return None


async def _down_since(pool: asyncpg.Pool, helper: str) -> float | None:
    return _as_stamp(await database.get_state(pool, DOWN_SINCE_KEY.format(helper=helper)))


async def _last_alert(pool: asyncpg.Pool, helper: str) -> float | None:
    """When this helper last paged, or ``None`` for "never".

    Not zero: "never" and "at epoch zero" are different facts, and the difference is
    the cooldown — a helper that has never paged must not have to sit out an hour
    that began before the bot existed.
    """
    return _as_stamp(await database.get_state(pool, ALERTED_AT_KEY.format(helper=helper)))


async def watch(
    pool: asyncpg.Pool,
    settings: Settings,
    *,
    bot: Bot | None = None,
    admin_ids: Iterable[int] = (),
    now: float | None = None,
) -> WatchResult:
    """One sweep: record changes, then decide whether anyone has to hear about it.

    ``bot=None`` records and reports without sending anything (a diagnostic run, or
    ``scripts/smoke.py``): the history is the part that must never depend on a chat
    being available.
    """
    states = await observe(settings)
    transitions: list[HelperState] = []
    alerted: list[HelperState] = []
    recovered: list[HelperState] = []
    stamp = _now_iso() if now is None else f"{now:.3f}"

    for state in states:
        previous = await database.last_helper_event(pool, state.helper)
        # Compared on *state*, and the reason rides along with the row: a rotated
        # token changes the reason while the route stays healthy, and a history full
        # of "nothing changed" rows is a history nobody reads.
        if previous is None or previous[0] != state.state:
            transitions.append(state)
            await database.record_helper_event(
                pool,
                helper=state.helper,
                state=state.state,
                reason=state.reason,
            )

        moment = float(stamp)
        if state.failing:
            started = await _down_since(pool, state.helper)
            if started is None:
                # First sighting of this outage: the clock starts now, so the wait
                # before paging is measured rather than assumed.
                await database.set_state(
                    pool, DOWN_SINCE_KEY.format(helper=state.helper), stamp
                )
                started = moment
            last_alert = await _last_alert(pool, state.helper)
            if (
                bot is not None
                and (moment - started) >= ALERT_AFTER_S
                and (last_alert is None or (moment - last_alert) >= ALERT_COOLDOWN_S)
            ):
                alerted.append(state)
                await database.set_state(
                    pool, ALERTED_AT_KEY.format(helper=state.helper), stamp
                )
        else:
            started = await _down_since(pool, state.helper)
            if started is not None:
                await database.set_state(
                    pool, DOWN_SINCE_KEY.format(helper=state.helper), ""
                )
                if bot is not None and state.state in {"ok", "warming"}:
                    recovered.append(state)

    if bot is not None and admin_ids:
        if alerted:
            await _send(bot, admin_ids, render_alert(tuple(alerted)))
        if recovered:
            await _send(bot, admin_ids, render_recovery(tuple(recovered)))

    return WatchResult(
        states=states,
        transitions=tuple(transitions),
        alerted=tuple(alerted),
        recovered=tuple(recovered),
    )


async def _send(bot: Bot, admin_ids: Iterable[int], text: str) -> None:
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 — one unreachable admin is not an outage
            logger.warning("could not send a helper alert to %s", admin_id)


def render_alert(states: tuple[HelperState, ...]) -> str:
    """The page: which helper, for how long, what it costs, and the one command."""
    lines = ["🔌 <b>یک هلپر یوتیوب از کار افتاده</b>", ""]
    for state in states:
        lines.append(f"❌ {state.label}")
        if state.reason:
            lines.append(f"   علت: {state.reason}")
        lines.append(f"   اثر: {HELPER_LOSS.get(state.helper, '')}")
        lines.append(f"   قدم بعدی: {HELPER_FIXES.get(state.helper, '/doctor')}")
    lines += [
        "",
        "بقیهٔ ربات سالم کار میکند؛ این مسیر یکی از راههای یوتیوب است، نه همهٔ آن — "
        "/doctor جزئیات زنده را دارد.",
    ]
    return "\n".join(lines)


def render_recovery(states: tuple[HelperState, ...]) -> str:
    """The other half of a page: an admin should not have to ask whether it came back."""
    lines = ["✅ <b>هلپر یوتیوب برگشت</b>", ""]
    for state in states:
        detail = f" — {state.reason}" if state.reason else ""
        lines.append(f"{state.label}: {state.state}{detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The periodic report
# ---------------------------------------------------------------------------


async def summary(
    pool: asyncpg.Pool, *, days: int = 7, now: datetime | None = None
) -> list[Outage]:
    """Downtime per helper inside a window, from the recorded transitions.

    A window that opens during an outage has no start transition inside it, so the
    first "down" is counted from the window's own start — an undercount, never an
    invention. A window that ends during one counts up to now, for the same reason.
    """
    since = (now or utcnow()) - timedelta(days=days)
    rows = await database.helper_events(pool, since)
    if not rows:
        return []

    now_ts = (now or utcnow()).timestamp()
    open_since: dict[str, float] = {}
    hours: dict[str, float] = {}
    counts: dict[str, int] = {}
    reasons: dict[str, str] = {}

    for created_at, helper, state, reason in rows:
        counts[helper] = counts.get(helper, 0) + 1
        if state == "down":
            open_since.setdefault(helper, created_at.timestamp())
            if reason:
                reasons[helper] = reason
        elif helper in open_since:
            hours[helper] = hours.get(helper, 0.0) + (
                created_at.timestamp() - open_since.pop(helper)
            ) / 3600

    for helper, started in open_since.items():
        hours[helper] = hours.get(helper, 0.0) + (now_ts - started) / 3600

    return [
        Outage(
            helper=helper,
            hours=round(value, 1),
            transitions=counts.get(helper, 0),
            last_reason=reasons.get(helper, ""),
        )
        for helper, value in sorted(hours.items(), key=lambda item: -item[1])
        if value > 0
    ]


def render_summary(outages: list[Outage]) -> str:
    """The digest's helper paragraph: quiet when nothing happened, named otherwise."""
    if not outages:
        return "🧩 هلپرها: در این بازه قطعیای ثبت نشده."
    lines = ["🧩 <b>هلپرها</b>"]
    for outage in outages:
        lines.append(f"• {outage.label}: {outage.hours} ساعت قطعی ({outage.transitions} تغییر وضعیت)")
        if outage.last_reason:
            lines.append(f"  آخرین علت: {outage.last_reason}")
        lines.append(f"  قدم بعدی: {HELPER_FIXES.get(outage.helper, '/doctor')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The loop the bot runs
# ---------------------------------------------------------------------------


async def run_helper_watch(
    stop_event: asyncio.Event,
    pool: asyncpg.Pool,
    bot: Bot | None = None,
    admin_ids: Iterable[int] = (),
    *,
    settings: Settings | None = None,
) -> None:
    """Sweep the helpers on a timer until shutdown asks it to stop.

    Own interval rather than the maintenance loop's hour: a helper that breaks should
    be reported in minutes, and the whole sweep is a few local requests.
    """
    settings = settings or get_settings()
    interval = settings.helper_watch_interval_s
    if interval <= 0:
        logger.info("helper watch: off (HELPER_WATCH_INTERVAL_S=0)")
        return
    admins = tuple(admin_ids)
    logger.info(
        "helper watch started (every %ss%s)",
        f"{interval:.0f}",
        f", {len(admins)} admin(s)" if admins else " — no admins to alert",
    )
    while not stop_event.is_set():
        try:
            result = await watch(pool, settings, bot=bot, admin_ids=admins)
            for state in result.transitions:
                logger.info("helper %s is now %s (%s)", state.helper, state.state, state.reason)
            for state in result.alerted:
                logger.warning("helper %s has been down long enough to page the admins", state.helper)
        except Exception:
            logger.exception("helper watch sweep failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    logger.info("helper watch stopped")


def describe(states: Iterable[HelperState]) -> str:
    """One line per helper, for scripts that want the same vocabulary as the report."""
    return " | ".join(f"{state.label}: {state.state}" for state in states)
