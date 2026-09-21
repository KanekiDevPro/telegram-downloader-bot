"""The admin panel's screens: four questions, answered from the live stack.

Everything here is *read*, never changed: the panel's job is to turn "how is it
going?" into one message an operator can read on a phone — users and downloads
today, whether the pieces the bot depends on are answering, how deep the queue is —
and to hand the two *actions* worth having (the doctor, the cookie re-export) to the
buttons that already exist for them.

One query for the numbers and one probe pass for the health, so opening the panel is
cheap enough to do while downloads are running. A dependency that does not answer is
reported as such rather than raised: a health screen that fails to render when
something is broken is the one screen that must never break.
"""

from __future__ import annotations

import logging

import asyncpg

from core import database
from core.config import Settings, probe_url
from core.i18n import DEFAULT_LANG, t
from core.utils import today_local
from services.cobalt import CobaltService
from services.doctor import fallback_health, http_reachable
from services.queue import TaskQueue

logger = logging.getLogger(__name__)

#: How long a helper probe may take when the panel is opened. Short: four lines of
#: report must not become a ten-second wait on a phone.
PROBE_TIMEOUT_S = 3.0

#: The fallback engine's states, as the panel spells them (see doctor.FALLBACK_TITLES
#: for the states themselves — this only renames them for a bilingual screen).
_COBALT_KEYS: dict[str, str] = {
    "ready": "panel.cobalt.ready",
    "quarantined": "panel.cobalt.quarantined",
    "auth": "panel.cobalt.auth",
    "youtube": "panel.cobalt.youtube",
    "unreachable": "panel.cobalt.unreachable",
    "degraded": "panel.cobalt.degraded",
    "off": "panel.cobalt.off",
    "unknown": "panel.cobalt.unknown",
}


def cobalt_state_text(state: str, lang: str = DEFAULT_LANG) -> str:
    """The fallback's state in the reader's language (unknown states as they are)."""
    key = _COBALT_KEYS.get(state)
    return t(key, lang) if key else state


async def header(pool: asyncpg.Pool, lang: str = DEFAULT_LANG) -> str:
    """The panel's first screen: title, a line of context, and the queue at a glance."""
    stats = await database.admin_stats(pool, today_local())
    return "\n".join(
        (
            t("admin.title", lang),
            "",
            t("admin.subtitle", lang),
            "",
            t(
                "admin.stats_brief",
                lang,
                users=stats["users"],
                downloads_today=stats["downloads_today"],
                blocks_24h=stats["blocks_24h"],
            ),
        )
    )


async def stats_text(pool: asyncpg.Pool, lang: str = DEFAULT_LANG) -> str:
    """Users, downloads, cache, failures, pending payments — one query."""
    stats = await database.admin_stats(pool, today_local())
    try:
        counts = await database.language_counts(pool)
        languages = ", ".join(
            t("admin.language_count", lang, language=row["language"], count=row["count"])
            for row in counts
        ) or "—"
    except Exception:  # a missing breakdown must not hide the numbers above it
        logger.exception("could not read the language breakdown")
        languages = "—"
    return t(
        "admin.stats",
        lang,
        users=stats["users"],
        premium=stats["premium"],
        new_users=stats["new_users"],
        downloads_today=stats["downloads_today"],
        active_today=stats["active_today"],
        cache_rows=stats["cache_rows"],
        blocks_24h=stats["blocks_24h"],
        pending_txns=stats["pending_txns"],
        languages=languages,
    )


async def queue_text(
    queue: TaskQueue, settings: Settings, lang: str = DEFAULT_LANG
) -> str:
    """How much work is waiting, and how many hands there are for it."""
    try:
        depth = await queue.depth()
    except Exception:  # Redis down is exactly what an operator is looking for
        logger.exception("could not read the queue depth")
        depth = -1
    return t(
        "admin.queue",
        lang,
        depth="?" if depth < 0 else depth,
        workers=settings.worker_count,
        backend=settings.queue_backend,
    )


async def health_text(
    pool: asyncpg.Pool,
    queue: TaskQueue,
    settings: Settings,
    cobalt: CobaltService | None,
    lang: str = DEFAULT_LANG,
) -> str:
    """Database, queue, and the three services a download may depend on."""
    database_ok = await _database_ok(pool)
    try:
        depth = await queue.depth()
        redis_ok = True
    except Exception:
        logger.exception("could not reach the queue backend")
        depth = 0
        redis_ok = False

    cobalt_line = await _cobalt_line(pool, settings, cobalt, lang)
    pot_line = await _helper_line(
        "panel.pot_line",
        settings.ytdlp_pot_provider_url,
        lang,
        name="PO-token provider",
    )
    session_line = await _helper_line(
        "panel.session_line",
        settings.youtube_session_server,
        lang,
        name="YouTube session server",
    )
    return t(
        "admin.health",
        lang,
        database=_state(database_ok, lang),
        redis=_state(redis_ok, lang),
        depth=depth,
        workers=settings.worker_count,
        cobalt=cobalt_line,
        pot=pot_line,
        session=session_line,
    )


def tools_text(lang: str = DEFAULT_LANG) -> str:
    """What the buttons on this screen do, and how to reach them without them."""
    return t("admin.tools", lang)


async def _database_ok(pool: asyncpg.Pool) -> bool:
    try:
        return await pool.fetchval("SELECT 1") == 1
    except Exception:
        logger.exception("database check failed")
        return False


def _state(ok: bool, lang: str) -> str:
    return t("admin.check_ok" if ok else "admin.check_fail", lang)


async def _cobalt_line(
    pool: asyncpg.Pool, settings: Settings, cobalt: CobaltService | None, lang: str
) -> str:
    """The fallback's line: state, address, and the dialect it answered in."""
    try:
        health = await fallback_health(settings, cobalt, probe=True, pool=pool)
    except Exception:
        logger.exception("could not probe the fallback engine")
        return t("panel.cobalt.error", lang)
    return t(
        "panel.cobalt_line",
        lang,
        icon=health.icon,
        state=cobalt_state_text(health.state, lang),
        url=health.url or "—",
        dialect=health.dialect or "—",
        where=t("panel.embedded", lang) if health.embedded else t("panel.remote", lang),
    )


async def _helper_line(
    key: str, configured: str, lang: str, *, name: str
) -> str:
    """One helper server: where it is, and whether this process can reach it."""
    if not configured:
        return t("panel.helper_off", lang, name=name)
    target = probe_url(configured)
    try:
        reachable = await http_reachable(target, timeout=PROBE_TIMEOUT_S)
    except Exception:
        logger.exception("could not probe %s at %s", name, target)
        reachable = False
    return t(key, lang, name=name, state=_state(reachable, lang), url=target)
