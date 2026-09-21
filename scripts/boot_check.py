"""Offline wiring check: builds the whole app, verifies the graph, shuts down.

Needs a reachable Postgres (``docker compose up -d postgres``). Redis is
optional — when it is missing the bot must fall back to the in-memory queue.

No Telegram network calls are made: ``BOT_TOKEN`` is only used to construct the
``Bot`` object (which validates the shape, not the credentials).

Usage:  python scripts/boot_check.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Keep the check offline even when a real token sits in .env.
os.environ.setdefault("BOT_TOKEN", "123456789:OFFLINE-WIRING-CHECK-TOKEN")

from core import database  # noqa: E402
from core.catalog import MESSAGES  # noqa: E402
from core.config import Settings, get_settings, probe_url  # noqa: E402
from core.i18n import LANGS  # noqa: E402
from core.logging import setup_logging  # noqa: E402
from core.telegram_api import session_target  # noqa: E402
from core.utils import DEFAULT_VIDEO_QUALITY, quality_key  # noqa: E402
from handlers import admin as admin_service  # noqa: E402
from main import build_app, shutdown  # noqa: E402
from services import (
    cobalt_cookies,  # noqa: E402
    cookie_refresh,  # noqa: E402
    login_wizard,  # noqa: E402
    proxy_health,  # noqa: E402
)
from services import doctor as doctor_service  # noqa: E402
from services import fallback as fallback_service  # noqa: E402
from services import worker as worker_service  # noqa: E402
from services.cobalt import url_is_fetchable  # noqa: E402
from services.cobalt_cookies import COBALT_FILE_NAME  # noqa: E402
from services.cookie_watch import DOCTOR_CALLBACK, REFRESH_CALLBACK  # noqa: E402
from services.doctor import (  # noqa: E402
    COBALT_COOKIE_CHECK_NAME,  # noqa: E402
    FALLBACK_CHECK_NAME,
    POT_CHECK_NAME,
    SESSION_CHECK_NAME,
    FallbackHealth,
    mount_wording,
    probe_pot_provider,
    probe_session_server,
)
from services.doctor import run_youtube_doctor as run_doctor  # noqa: E402
from services.extractor import (  # noqa: E402
    BrowserSpecError,
    ExtractionError,
    browser_profile_reachable,
    login_looking_block,
    missing_youtube_login_cookies,
    parse_browser_spec,
    pot_plugin_installed,
    pot_plugin_version,
)
from services.preflight import youtube_preflight  # noqa: E402
from services.queue import BLOCK_TIMEOUT_S  # noqa: E402

RESULTS: list[bool] = []
WARNINGS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" - {detail}" if detail else ""))
    return bool(ok)


def warn(name: str, detail: str = "") -> None:
    """A degraded-but-working condition: the bot runs, with less capability."""
    WARNINGS.append(name)
    print(f"  WARN {name}" + (f" - {detail}" if detail else ""))


async def _http_reachable(base_url: str, timeout: float = 5.0) -> bool:
    """Does a helper server (Bot API / PO-token provider) answer on this network?"""
    import aiohttp

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as session:
            async with session.get(base_url) as response:
                return response.status < 500
    except Exception:
        return False


#: What "fast enough" means for a local helper under a handful of parallel calls.
#: These servers answer from memory; a helper that needs seconds for six local
#: requests is one that will stall downloads when the stack is busy.
HELPER_LOAD_BUDGET_S = 3.0


async def _helper_load(
    base_url: str, *, path: str, count: int
) -> tuple[list[float], list[str]]:
    """Fire ``count`` requests at one helper at once; report each time and outcome.

    Timing rather than a single "ok", because the interesting failure is not a dead
    helper (that is a different check) but a *slow* one: the bot's workers need these
    answers while a user is waiting, and half a minute of queueing looks exactly like
    a hung download from the outside.
    """
    import aiohttp

    results: list[tuple[float, str]] = []

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HELPER_LOAD_BUDGET_S * 4)
    ) as session:

        async def one() -> None:
            started = time.perf_counter()
            try:
                async with session.get(f"{probe_url(base_url)}{path}") as response:
                    await response.read()
                    results.append((time.perf_counter() - started, f"HTTP {response.status}"))
            except Exception as exc:  # noqa: BLE001 — the failure is the finding
                results.append((time.perf_counter() - started, type(exc).__name__))

        await asyncio.gather(*(one() for _ in range(count)))

    answered = [seconds for seconds, note in results if note.startswith("HTTP")]
    return answered, [note for _, note in results]


def _mask_credentials(url: str) -> str:
    """A proxy URL without its password: this output ends up in screenshots."""
    if "@" not in url:
        return url
    head, _, tail = url.rpartition("@")
    scheme, _, credentials = head.partition("://")
    user, _, _password = credentials.partition(":")
    return f"{scheme}://{user}:***@{tail}"


def _env_value(name: str) -> str:
    """One raw value from `.env`.

    Some settings exist only on the compose side (COBALT_HTTP_PROXY is read by the
    cobalt *service*, never by this process), so they are not in Settings — but they
    are still what an operator has to be told about.
    """
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{name}="):
            return stripped.partition("=")[2].strip()
    return ""


#: The same public video the doctor probes with: long-lived, never region-locked.
PROBE_VIDEO = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"

#: A link the instance serves with **no session of its own** — the same one smoke
#: probes. YouTube would fail here on a zero-config stack (cobalt has no login), so
#: probing it would report a healthy embedded instance as a problem.
NEUTRAL_PROBE = "https://streamable.com/moo"


async def _cobalt_probe(cobalt: Any) -> tuple[bool, str]:
    """Ask the fallback instance for one link — a resolution, not a download.

    The *shape* of the answer is the check (``url_is_fetchable``): that URL is what
    the bot will download from, and a third party wrote it.
    """
    started = time.monotonic()
    try:
        media = await cobalt.resolve(NEUTRAL_PROBE, "video")
    except Exception as exc:  # noqa: BLE001 — network/instance dependent by design
        return False, fallback_service.describe(exc)
    return (
        url_is_fetchable(media.url),
        f"resolved in {time.monotonic() - started:.1f}s → {media.url.split('?')[0][:60]}",
    )


async def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    print(f"== boot check (db={settings.database_url.split('@')[-1]}) ==")

    # No digest: a wiring check must not be able to send a report (or consume its
    # window) on whatever database it is pointed at.
    app = await build_app(send_digest=False)
    try:
        pool = app["pool"]
        dp = app["dp"]
        queue = app["queue"]
        extractor = app["extractor"]

        check("database is reachable", await pool.fetchval("SELECT 1") == 1)

        tables = {
            row[0]
            for row in await pool.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        required = {
            "users",
            "subscription_plans",
            "transactions",
            "smart_cache",
            "block_events",
            "bot_state",
            "fix_events",
        }
        check("schema bootstrap created every table", required <= tables, str(sorted(required & tables)))

        plans = await database.list_plans(pool)
        check("subscription plans are seeded", len(plans) >= 3, f"count={len(plans)}")

        check("routers are registered", len(dp.sub_routers) >= 3, f"count={len(dp.sub_routers)}")
        check(
            "admin /doctor is wired to the extractor",
            "extractor" in dp.workflow_data and app["extractor"] is dp["extractor"],
        )
        admin_router = next((r for r in dp.sub_routers if r.name == "admin"), None)
        check(
            "admin /blocks is wired to the block digest",
            admin_router is not None
            and any(
                getattr(handler.callback, "__name__", "") == "cmd_blocks"
                for handler in admin_router.message.handlers
            ),
        )
        check(
            "the cookie alert's check-now button reaches the doctor",
            admin_router is not None
            and any(
                getattr(handler.callback, "__name__", "") == "on_alert_check"
                for handler in admin_router.callback_query.handlers
            ),
            f"callback {DOCTOR_CALLBACK}",
        )
        handlers = {
            getattr(handler.callback, "__name__", "")
            for handler in (admin_router.message.handlers if admin_router else ())
        }
        check(
            "the admin panel and its commands are registered",
            {"cmd_admin", "cmd_refresh", "cmd_trend", "cmd_fixlogin"} <= handlers,
            "/admin (panel), /refresh (on-demand export), /trend (did the fix help), "
            "/fixlogin (the guide)",
        )

        # The bilingual layer: a key without one of its two languages is a user
        # reading a raw identifier, and it is invisible until someone switches.
        incomplete = [
            key
            for key, entry in MESSAGES.items()
            if any(not entry.get(lang) for lang in LANGS)
        ]
        check(
            "every message exists in every language",
            not incomplete,
            f"{len(MESSAGES)} keys x {len(LANGS)} languages"
            if not incomplete
            else f"missing: {incomplete[:3]}",
        )

        # Quality tiers: they must exist in the *cache key* space (so a 480p ask
        # cannot replay a 1080p file) without moving the default tier's key — moving
        # it would orphan every row written before tiers existed.
        from services import cache as cache_service

        probe_url_key = "https://youtu.be/probe"
        default_key = cache_service.cache_key(probe_url_key, "video")
        check(
            "quality tiers separate cache entries without orphaning the old ones",
            default_key
            == cache_service.cache_key(probe_url_key, "video", DEFAULT_VIDEO_QUALITY)
            and len(
                {
                    cache_service.cache_key(probe_url_key, "video", tier)
                    for tier in ("best", "1080", "720", "480")
                }
            )
            == 4
            and quality_key("video", "480") == "video:480",
            "default key unchanged; best/1080/720/480 distinct",
        )

        # The welcome screen's service list is the first thing a new user reads, and
        # the *only* place a platform is named before a link is sent — a translation
        # that dropped one would look like the bot no longer supports it.
        from core.i18n import t as translate

        expected = {
            "en": ("YouTube", "Instagram", "TikTok", "Spotify", "SoundCloud", "Reddit"),
            "fa": ("یوتیوب", "اینستاگرام", "تیک‌تاک", "اسپاتیفای", "ساندکلاود", "ریدیت"),
        }
        welcome_missing = [
            f"{lang}/{name}"
            for lang, names in expected.items()
            for name in names
            if name not in translate("start.welcome", lang, name="x")
        ]
        check(
            "the welcome screen names the supported services in both languages",
            not welcome_missing,
            ", ".join(welcome_missing) or "YouTube/Instagram/TikTok/Spotify/SoundCloud/Reddit",
        )
        user_router = next((r for r in dp.sub_routers if r.name == "user"), None)
        user_commands = {
            getattr(handler.callback, "__name__", "")
            for handler in (user_router.message.handlers if user_router else ())
        }
        check(
            "the user frontend is wired (profile, premium, help, status, language)",
            {"cmd_start", "cmd_profile", "cmd_premium", "cmd_help", "cmd_status", "cmd_language"}
            <= user_commands,
            "/profile، /premium، /help — the menu's screens plus /status and /language",
        )
        user_callbacks = {
            getattr(handler.callback, "__name__", "")
            for handler in (user_router.callback_query.handlers if user_router else ())
        }
        check(
            "every menu button has a handler registered",
            {
                "on_menu_profile",
                "on_menu_premium",
                "on_menu_help",
                "on_menu_language",
                "on_menu_home",
            }
            <= user_callbacks,
            "👤 پروفایل من، 💎 ارتقا به ویژه (VIP)، ❓ راهنما، 🌐 زبان، 🔙 بازگشت",
        )
        check(
            "the cookie alert's export button reaches the same path as /refresh",
            admin_router is not None
            and any(
                getattr(handler.callback, "__name__", "") == "on_alert_refresh"
                for handler in admin_router.callback_query.handlers
            ),
            f"callback {REFRESH_CALLBACK}",
        )

        expected_workers = settings.worker_count + 1  # + always-on maintenance
        watcher = app["cookie_watch"]
        watching = watcher.enabled and bool(settings.admin_ids)
        if watching:
            expected_workers += 1  # + the cookie-jar watcher
        helper_watching = settings.helper_watch_interval_s > 0
        if helper_watching:
            expected_workers += 1  # + the helper watch
        workers = app["workers"]
        check(
            f"workers running ({settings.worker_count} download + maintenance)",
            len(workers) == expected_workers and all(not task.done() for task in workers),
            f"count={len(workers)}",
        )

        # A replaced cookie jar is picked up by the next download, silently. The
        # watcher is what turns that into a message instead of a /doctor session.
        if watching:
            check(
                "cookie-jar watcher will alert admins about a fresh export",
                any(task.get_name() == "cookie-watch" for task in workers),
                f"every {watcher.interval_s:.0f}s → {len(settings.admin_ids)} admin(s)",
            )
        elif not watcher.enabled:
            print("  note  cookie-jar watcher: off (COOKIE_WATCH_INTERVAL_S=0)")

        else:
            warn(
                "cookie-jar watcher cannot alert anyone — ADMIN_IDS is empty",
                "a fresh cookies.txt would only be visible in the log",
            )

        # The helpers are watched on their own timer, because a dead one is a
        # failure a *user* cannot describe: their link just fails, and it looks
        # exactly like an IP block. The sweep is what turns that into a history, a
        # page and a paragraph in the weekly digest.
        if helper_watching:
            check(
                "the two YouTube helpers are watched on a timer",
                any(task.get_name() == "helper-watch" for task in workers),
                f"every {settings.helper_watch_interval_s:.0f}s — one row per change, "
                "a page after 10 minutes down, and a weekly paragraph",
            )
        else:
            print("  note  helper watch: off (HELPER_WATCH_INTERVAL_S=0)")

        # A login-shaped block is the one failure an operator can fix, and half of
        # that fix is mechanical. With a profile named, the failure path does it:
        # re-export, replace, probe, report. Off unless COOKIE_AUTO_EXPORT is set —
        # and checked against the failure path itself, not just the import.
        refresh_spec = settings.cookie_auto_export.strip()
        if not refresh_spec:
            print(
                "  note  automatic cookie refresh: off (COOKIE_AUTO_EXPORT unset) — "
                "a login-shaped block reports without re-exporting"
            )
        elif settings.cookie_file is None:
            warn(
                "COOKIE_AUTO_EXPORT is set but COOKIE_FILE is not",
                "there is no jar to replace — set COOKIE_FILE (or COOKIES_HOST_DIR) too",
            )
        else:
            try:
                refresh_browser = parse_browser_spec(refresh_spec)[0]
            except BrowserSpecError as exc:
                warn(
                    "COOKIE_AUTO_EXPORT is not a usable browser profile",
                    f"{refresh_spec!r}: {exc}",
                )
            else:
                check(
                    "a login-shaped block would re-export the jar automatically",
                    "cookie_refresh.auto_refresh_jar(" in inspect.getsource(worker_service),
                    f"{refresh_browser} → {settings.cookie_file}, at most every "
                    f"{cookie_refresh.DEFAULT_COOLDOWN_S / 60:.0f} min (never overwrites "
                    "a working login, probes the result, reports it)",
                )
                # The reachability pre-check, answered here rather than on the
                # first failing download. Inside a container "not here" is the
                # expected answer, and the admins are told to export on the host.
                if browser_profile_reachable(refresh_spec) is False:
                    print(
                        "  note  no browser profile on this machine — a login-shaped "
                        "block will report that (and where to export from) instead of "
                        "attempting it"
                    )

        check("queue backend responds", await queue.depth() >= 0, type(queue).__name__)

        check("download directory exists", extractor.download_dir.is_dir(), str(extractor.download_dir))

        # The cookie jar is mounted, so it must never be baked into the image (a
        # fresh export has to win after a restart, not a rebuild).
        compose_file = PROJECT_ROOT / "docker-compose.yml"
        if compose_file.is_file():
            compose = compose_file.read_text(encoding="utf-8")
            check(
                "compose reads the cookie jar from a read-only mount",
                ":/cookies:ro" in compose and "COOKIE_FILE: /cookies/cookies.txt" in compose,
            )
        print(f"  note  ffmpeg available: {extractor.ffmpeg_available} (needed for MP3)")
        print(f"  note  cookies: {settings.cookie_file if extractor.using_cookies else 'none'}")
        if extractor.using_cookies:
            # Where the jar came from and whether the copy yt-dlp reads is
            # current: the two questions behind "I re-exported, did it take?".
            jar = extractor.cookie_jar_state()
            print(
                f"  note  cookie jar: {mount_wording(jar)}, {jar.cookie_count} cookies, "
                f"in sync: {jar.in_sync}"
            )
        if extractor.using_cookies:
            # The same diagnosis the wizard and /fixlogin start from, so an
            # operator knows what the chat would say before a link fails.
            print(f"  note  /fixlogin would say: {login_wizard.diagnose(extractor).summary}")
        if settings.cookie_file is not None and not settings.cookie_file.exists():
            # Expected on a fresh clone: the jar is optional, and the bot runs
            # without it for every site that does not need a login.
            warn(
                "COOKIE_FILE does not exist — running without cookies",
                f"{settings.cookie_file}: export it (scripts/export_cookies.py) then "
                "`docker compose restart bot`, no rebuild needed",
            )
        missing_login = (
            missing_youtube_login_cookies(settings.cookie_file) if extractor.using_cookies else ()
        )
        if missing_login:
            # Not a failure — every other site still works — but it is the one
            # problem that silently masquerades as a blocked IP.
            warn(
                "cookies carry no YouTube login — YouTube blocks will look like an IP ban",
                f"missing {', '.join(missing_login)}; re-export while signed in "
                "(scripts/export_cookies.py)",
            )
        elif extractor.youtube_login_ready:
            print("  note  cookies carry a YouTube login (LOGIN_INFO + SAPISID)")

        # Which failure a blocked YouTube link would look like *here*, with the
        # jar as configured: the users' message and the admins' notice follow
        # this rule, and it is the difference between a shrug and a fix.
        if login_looking_block(
            ExtractionError("EXTRACTOR_BLOCKED", "Sign in to confirm you're not a bot"),
            "https://youtu.be/abc",
            settings.cookie_file,
        ):
            print(
                "  note  a blocked YouTube link would be blamed on the jar (missing "
                f"{', '.join(missing_login) or 'cookies'}), not on the IP"
            )
        else:
            print(
                "  note  a blocked YouTube link would go down as the site/IP refusing "
                "(the jar cannot explain it)"
            )

        # What happens to a YouTube link *right now*: queued with a note about the
        # login, or held back because a refusal was actually observed.
        verdict = youtube_preflight("https://youtu.be/abc", settings.cookie_file)
        print(f"  note  a fresh YouTube link would be: {verdict.status} — {verdict.message or 'no comment'}")

        # The fallback engine: yt-dlp stays primary, and a *refusal* — the one
        # failure this host cannot fix — goes to another address instead of back to
        # the user. Held back links are the case it changes most, so that decision
        # is checked rather than described.
        cobalt = app.get("cobalt")
        check(
            "the fallback extractor is wired into the download path",
            cobalt is not None
            and cobalt.enabled == settings.cobalt_enabled
            and "cobalt" in inspect.signature(worker_service.run_worker).parameters
            and "cobalt" in inspect.signature(worker_service.process_download_task).parameters,
            # The address the bot itself would use — on the host that is the published
            # loopback port, not the compose name, and saying so here is what stops
            # "unreachable" from looking like a broken deployment.
            f"{(cobalt.pool_label() if cobalt is not None else settings.cobalt_api_url) or '(off)'}"
            + (" +api key" if settings.cobalt_api_key else ""),
        )
        check(
            "a flagged host still has a second address to try",
            len(settings.cobalt_endpoints) >= 2,
            " | ".join(settings.cobalt_endpoints) or "(no pool)",
        )
        if cobalt is not None and cobalt.enabled:
            check(
                "only a site refusal is handed over",
                fallback_service.should_use_fallback(
                    ExtractionError("EXTRACTOR_BLOCKED", "blocked"), cobalt
                )
                and fallback_service.should_use_fallback(
                    ExtractionError("SESSION_STALE", "stale"), cobalt
                )
                and not fallback_service.should_use_fallback(
                    ExtractionError("GEO_RESTRICTED", "geo"), cobalt
                ),
                "blocked/stale → fallback, private/geo/live → the user's message",
            )
            held_back = youtube_preflight(
                "https://youtu.be/abc", settings.cookie_file, fallback_available=True
            )
            print(
                "  note  a link the preflight would hold back is now "
                f"'{held_back.status}' with the fallback enabled"
            )
            probed, detail = await _cobalt_probe(cobalt)
            if probed:
                check("the fallback instance serves a link on its own", True, detail)
            else:
                warn(
                    "the fallback instance could not resolve a probe link",
                    f"{detail} — a blocked link stays an error until it answers",
                )
        else:
            print(
                "  note  fallback extractor: off (COBALT_API_URL empty) — a blocked link "
                "is answered exactly as before"
            )

        # The same fallback facts, through the *report* an admin actually reads:
        # the section has to be in /doctor, not only in this script.
        offline = await run_doctor(settings, extractor, cobalt=cobalt, probe=False)
        rendered = offline.render()
        lines = rendered.splitlines()
        index = next((i for i, line in enumerate(lines) if FALLBACK_CHECK_NAME in line), None)
        detail = lines[index].strip() if index is not None else "(missing)"
        if index is not None and "علت:" in lines[index + 1]:
            # The reason is its own line in the report (phone screens are narrow);
            # a check line is one line, so it is folded back in here.
            detail += " | " + lines[index + 1].strip()
        check(
            "the doctor's report carries the fallback as its own section",
            index is not None,
            detail[:150],
        )
        check(
            "the doctor commands hand the running fallback client to that report",
            "cobalt" in inspect.signature(admin_service.cmd_doctor).parameters
            and "pool" in inspect.signature(admin_service.cmd_doctor).parameters
            and "cobalt" in inspect.signature(admin_service.on_alert_check).parameters,
            "live state (including a quarantine), not a fresh opinion",
        )

        # /blocks is the other command an admin reads the net's health from. It
        # needs the running client too, and its footer has to come from the *same*
        # verdict code the doctor renders — two renderers would eventually disagree
        # about the same instance.
        check(
            "/blocks reports the net's health without spending a probe",
            "cobalt" in inspect.signature(admin_service.cmd_blocks).parameters
            and "pool" in inspect.signature(admin_service.cmd_blocks).parameters
            and admin_service.fallback_health is doctor_service.fallback_health,
            "instance, state, and the last time a blocked link needed it",
        )
        net_url = settings.cobalt_api_url or "https://api.cobalt.example"
        sample = FallbackHealth(
            "quarantined", net_url, reason="ERROR: this instance wants a key"
        )
        skipped = sample.line()
        check(
            "a net that was skipped says so, with the reason",
            skipped.startswith(f"🔌 {sample.label}:")
            and "🟡" in skipped.splitlines()[0]
            and "ERROR: this instance wants a key" in skipped
            and "قدم بعدی" in skipped,
            skipped.splitlines()[0][:140],
        )

        # Zero-config means two things, and both are checked here: the stack ships
        # an instance, and the *default* points at it — a deployment that requires
        # the operator to find and configure a fallback is not zero-config.
        compose_text = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        check(
            "the stack ships its own fallback instance",
            "ghcr.io/imputnet/cobalt" in compose_text
            and "cobalt:" in compose_text
            and "API_URL: http://cobalt:9000/" in compose_text,
            "compose service `cobalt` (ghcr.io/imputnet/cobalt:10, API_PORT 9000)",
        )
        default_url = str(Settings.model_fields["cobalt_api_url"].default)
        check(
            "that instance is the default, so nothing has to be configured",
            default_url == "http://cobalt:9000",
            f"COBALT_API_URL default = {default_url}",
        )
        check(
            "the fallback says whether it is the embedded one",
            sample.embedded == settings.cobalt_embedded,
            f"{net_url} → {'داخلی' if sample.embedded else 'خارجی'}",
        )

        # The same zero-config deal for the two helpers on YouTube's no-login route:
        # shipped by the stack and pointed at by default. Unlike cobalt these may be
        # absent without breaking a download, so the *wiring* is asserted here and
        # their live answers are reported further down.
        check(
            "the stack ships the two YouTube helpers, on by default",
            "brainicism/bgutil-ytdlp-pot-provider" in compose_text
            and "ghcr.io/imputnet/yt-session-generator:webserver" in compose_text
            and 'profiles: ["pot"]' not in compose_text
            and 'profiles: ["yt-session"]' not in compose_text,
            "pot-provider + yt-session-generator (no profile gate)",
        )
        # Chromium in a container with Docker's 64 MB /dev/shm dies on startup,
        # and this helper's process then exits and is restarted every few seconds —
        # a loop that still answers `/token` in between, so only the setting (and
        # the log) reveals it. Asserted because the failure is otherwise silent.
        check(
            "the browser-based helper gets the shared memory it needs",
            'shm_size: "1g"' in compose_text,
            "shm_size 1g on yt-session-generator (Chromium dies on the 64 MB default)",
        )
        pot_default = str(Settings.model_fields["ytdlp_pot_provider_url"].default)
        session_default = str(Settings.model_fields["youtube_session_server"].default)
        check(
            "both helpers are the defaults, so nothing has to be configured",
            pot_default == "http://pot-provider:4416"
            and session_default == "http://yt-session-generator:8080",
            f"YTDLP_POT_PROVIDER_URL={pot_default} | YOUTUBE_SESSION_SERVER={session_default}",
        )
        check(
            "one setting hands the session server to the engine that reads it",
            "YOUTUBE_SESSION_SERVER: ${YOUTUBE_SESSION_SERVER:-http://yt-session-generator:8080}"
            in compose_text,
            "cobalt loads the token from the same address the doctor probes",
        )

        # The fallback's own login, generated from the jar the bot already has.
        # Checked the same way as the instance itself: the wiring is an assertion
        # (compose + the default), and the result on disk is a *report* — a file
        # nobody generated yet is not a broken deployment.
        cookie_state = cobalt_cookies.sync_from_jar(settings)
        check(
            "one jar signs both engines in (a generate-on-export default)",
            Settings.model_fields["cobalt_cookies_dir"].default is not None
            and "COBALT_COOKIES_DIR: /app/cobalt" in compose_text
            and f"COOKIE_PATH: /cookies/{COBALT_FILE_NAME}" in compose_text
            and "/app/cobalt" in compose_text,
            f"COBALT_COOKIES_DIR default = {Settings.model_fields['cobalt_cookies_dir'].default}",
        )
        if cookie_state.off:
            print(
                "  note  cobalt cookies: off (COBALT_COOKIES_DIR is empty) — the fallback "
                "runs without one"
            )
        else:
            document: object = {}
            try:
                document = json.loads(Path(cookie_state.path).read_text(encoding="utf-8"))  # type: ignore[arg-type]
            except (OSError, ValueError):
                document = {}
            entries = document.get("youtube") if isinstance(document, dict) else None
            if cookie_state.usable:
                shape_ok = isinstance(entries, list) and bool(entries) and all(
                    isinstance(entry, str) for entry in entries
                )
                kept = ""
                if cookie_state.other_services:
                    kept = f" | kept: {', '.join(cookie_state.other_services)}"
                check(
                    "the generated cookie file is the shape cobalt parses",
                    shape_ok,
                    f"{cookie_state.describe()}{kept}",
                )
            else:
                warn(
                    "no cobalt cookie file yet",
                    f"{cookie_state.describe()} — it is generated at startup and on every "
                    "export; the fallback serves the non-YouTube services either way",
                )
            if cobalt is not None and cobalt.enabled:
                started_at = await cobalt.server_start_time()
                if cobalt_cookies.restart_needed(cookie_state.generated_at, started_at) is True:
                    warn(
                        "cobalt is running an older cookie file",
                        "docker compose restart cobalt — it reads the file once, at startup",
                    )

        check(
            "the doctor reports that file as its own line",
            COBALT_COOKIE_CHECK_NAME in rendered,
            "🍪 کوکی کوبالت — what it holds, and whether the instance has it",
        )

        if settings.uses_local_api:
            credentials = settings.local_api_configured
            reachable = await _http_reachable(settings.telegram_api_base_url)
            if credentials and reachable:
                check(f"local Bot API server is ready at {session_target(settings)}", True)
            else:
                # Not a failure: connect_bot() logs the reason and runs on the
                # cloud API with the upload ceiling capped to 50 MB. Record the
                # same fallback so the reported limits match what the bot will do.
                settings.use_cloud_api_fallback()
                warn(
                    "local Bot API server is not usable — the bot falls back to the "
                    f"cloud API (max {settings.upload_limit_mb} MB uploads)",
                    f"credentials={credentials}, reachable={reachable}, "
                    f"start it with `docker compose --profile local-api up -d`",
                )
        else:
            print(
                "  note  Telegram API target: official cloud API "
                f"(uploads over 50 MB will fail; MAX_FILE_SIZE_MB={settings.max_file_size_mb})"
            )

        # The two helpers on YouTube's no-login route. Both are part of the default
        # stack and both can be missing without breaking a download — which is
        # exactly why their live answers are reported instead of assumed.
        plugin_version = pot_plugin_version()
        provider = None
        if settings.wants_pot_provider:
            plugin = pot_plugin_installed()
            provider = await probe_pot_provider(settings.ytdlp_pot_provider_url)
            if plugin and provider.reachable and not provider.error:
                check(
                    f"PO token provider answers at {settings.ytdlp_pot_provider_url}",
                    bool(provider.version),
                    f"v{provider.version} (plugin v{plugin_version})"
                    if provider.version
                    else "answered without a version",
                )
            else:
                warn(
                    "PO token provider is configured but cannot be used",
                    f"plugin_installed={plugin}, reachable={provider.reachable}, "
                    f"error={provider.error or '-'} — `docker compose up -d pot-provider`; "
                    "a download continues without a token, with one warning in the log",
                )
        else:
            print("  note  PO token provider: off (YTDLP_POT_PROVIDER_URL is empty)")

        session_server = None
        if settings.wants_session_server:
            session_server = await probe_session_server(settings.youtube_session_server)
            if session_server.ready:
                check(
                    f"the YouTube session server has a token at {settings.youtube_session_server}",
                    True,
                    f"{session_server.token_length} chars — cobalt reads it every 5 minutes",
                )
            else:
                warn(
                    "the YouTube session server has no token (the fallback's no-login "
                    "route for YouTube is dead until it has one)",
                    f"reachable={session_server.reachable}, error={session_server.error or '-'} — "
                    "`docker compose logs yt-session-generator`; the login jar and the "
                    "PO-token provider are the other two routes",
                )
        else:
            print("  note  YouTube session server: off (YOUTUBE_SESSION_SERVER is empty)")

        # Configured is not the same as wired: the engine has to carry the provider
        # in the option yt-dlp's plugin reads, or the setting stays decoration.
        if provider is not None:
            base_urls = extractor.extractor_args.get("youtubepot-bgutilhttp", {}).get("base_url")
            # The *reachable* address, not the setting: this is a host process, and
            # the engine has to carry the address yt-dlp can actually call.
            check(
                "the running engine is pointed at that provider",
                base_urls == [probe_url(settings.ytdlp_pot_provider_url)],
                f"extractor_args youtubepot-bgutilhttp:base_url={base_urls}",
            )

        # A cobalt that started before the token existed answers YouTube links with
        # `youtube.no_session_tokens` — and /doctor says so. Naming the cheap way out
        # (it re-reads the server every 5 minutes) beats "the fallback is broken".
        if session_server is not None and session_server.ready:
            health = await doctor_service.fallback_health(
                settings, cobalt, probe=False, probe_url=PROBE_VIDEO
            )
            if health.state == "youtube":
                warn(
                    "cobalt has not picked the session up yet",
                    "it re-reads the server every 5 minutes (or `docker compose restart "
                    "cobalt` now) — until then YouTube links fall through",
                )
            elif health.state in {"ready", "degraded"}:
                check(
                    "the fallback has its YouTube route in place",
                    True,
                    f"state: {health.title}",
                )
            else:
                print(
                    "  note  the fallback's YouTube session is not proven yet — "
                    "`python scripts/youtube_doctor.py` probes it live"
                )
        check(
            "the report has its own line for each helper",
            POT_CHECK_NAME in rendered and SESSION_CHECK_NAME in rendered,
            f"{POT_CHECK_NAME} | {SESSION_CHECK_NAME}",
        )

        # Both helpers under a little concurrent load, with the timing reported:
        # the bot does not ask one question at a time — two workers can need tokens
        # from the same helper at once — and a helper that answers the first request
        # while queueing the second is a download that stalls with nothing in the
        # log. Local traffic, so a number in the hundreds of milliseconds is the
        # only comfortable answer.
        for label, helper_url, helper_path in (
            (
                POT_CHECK_NAME,
                settings.ytdlp_pot_provider_url if settings.wants_pot_provider else "",
                doctor_service.POT_PING_PATH,
            ),
            (
                SESSION_CHECK_NAME,
                settings.youtube_session_server if settings.wants_session_server else "",
                doctor_service.SESSION_TOKEN_PATH,
            ),
        ):
            if not helper_url:
                continue
            timings, notes = await _helper_load(helper_url, path=helper_path, count=6)
            answered = f"{len(timings)}/6 answered in {min(timings):.2f}–{max(timings):.2f}s"
            check(
                f"{label} survives concurrent requests",
                len(timings) == 6 and max(timings) < HELPER_LOAD_BUDGET_S,
                f"{answered} ({', '.join(sorted(set(notes))) })",
            )

        # The proxy route: a clean IP is the only fix for a flagged one, and it does
        # not reach every engine. Which one it reaches is wiring, so it is asserted;
        # the one it cannot reach is said out loud instead of left to a surprise.
        # The *name* is the whole check: cobalt reads API_EXTERNAL_PROXY and
        # ignores HTTP_PROXY/HTTPS_PROXY (its undici dispatcher never looks at
        # them), so a compose file that sets the familiar pair wires nothing at all
        # — measured, not assumed: with a bogus API_EXTERNAL_PROXY cobalt fails to
        # reach YouTube, with a bogus HTTP_PROXY it reaches it as if unset.
        check(
            "each engine that can use a proxy is wired for one",
            "API_EXTERNAL_PROXY: ${COBALT_HTTP_PROXY:-}" in compose_text
            and "HTTP_PROXY: ${COBALT_HTTP_PROXY:-}" not in compose_text,
            "yt-dlp: YTDLP_PROXY | the fallback's egress: COBALT_HTTP_PROXY "
            "(becomes API_EXTERNAL_PROXY — the name cobalt reads) | its transfers: "
            "COBALT_PROXY",
        )
        check(
            "a proxy on the host has a name both containers can resolve",
            compose_text.count('"host.docker.internal:host-gateway"') >= 2,
            "bot + cobalt (so `host.docker.internal:<port>` works on Linux too)",
        )
        if settings.ytdlp_proxy:
            check(
                "the running engine carries the configured proxy",
                extractor.proxy == settings.ytdlp_proxy,
                _mask_credentials(settings.ytdlp_proxy),
            )
            if not _env_value("COBALT_HTTP_PROXY"):
                warn(
                    "the fallback has no proxy of its own",
                    "a blocked link handed to cobalt leaves through this host's address "
                    "— set COBALT_HTTP_PROXY for the *service* if that is the blocked one",
                )
        else:
            print(
                "  note  no proxy configured (YTDLP_PROXY empty) — the fix for an "
                "IP-level block is a *different kind* of address (a residential exit); "
                "a cloud one is what the check itself targets, and neither helper "
                "substitutes for either"
            )
        # The tunnel itself, asked live — this is the one route that, when it is
        # wrong, makes *every* download fail or silently keeps the address YouTube
        # already refused. Three different failures, three checks: it must answer,
        # the running engine must be carrying it, and it must leave from somewhere
        # other than this host.
        if settings.ytdlp_proxy:
            tunnel = await proxy_health.probe_with_retries(
                probe_url(settings.ytdlp_proxy), attempts=2, delay_s=1.0
            )
            check(
                "the yt-dlp tunnel answers",
                tunnel.reachable,
                tunnel.describe(),
            )
            check(
                "the running engine goes through the tunnel",
                extractor.using_proxy and extractor.proxy == probe_url(settings.ytdlp_proxy),
                _mask_credentials(settings.ytdlp_proxy)
                if extractor.using_proxy
                else "the engine is running direct (the boot probe dropped it)",
            )
            if tunnel.on_warp:
                direct = await proxy_health.direct_exit_ip()
                check(
                    "downloads leave from a different address than this host",
                    bool(direct) and direct != tunnel.exit_ip,
                    f"tunnel exit {tunnel.exit_ip}"
                    + (f" vs this host {direct}" if direct else " (host address unreadable)"),
                )
            elif tunnel.reachable and tunnel.traced:
                warn(
                    "the tunnel answers but is not on WARP",
                    f"warp={tunnel.warp or 'unknown'} — traffic still leaves from this "
                    "host's address; `docker compose logs warp` says why (and a WARP "
                    "licence key, WARP_LICENSE_KEY, changes the exit on some hosts)",
                )
            else:
                print(
                    "  note  the tunnel could not be asked where it goes (a SOCKS5 "
                    "proxy: aiohttp does not speak it) — reachability is all that was "
                    "verified; use http://warp:1080 for the full check"
                )
        print(
            "  note  the session server cannot be proxied by a *variable*: its token is "
            "bound to the public IP its browser ran on (that is what makes it "
            "*trusted*), which is why it shares the tunnel's network namespace instead"
        )

        # Workers must survive their blocking queue reads. redis-py's implicit
        # 5s socket timeout used to abort a longer BRPOP and kill every worker
        # a few seconds after startup — silent, and invisible to unit tests.
        idle_seconds = BLOCK_TIMEOUT_S + 1
        print(f"  ..    idling {idle_seconds}s to prove idle queue reads are harmless")
        await asyncio.sleep(idle_seconds)
        dead = [task for task in workers if task.done()]
        check(
            f"workers survive {idle_seconds}s of idle polling",
            not dead,
            ", ".join(f"{task.get_name()}: {task.exception()!r}" for task in dead) or "all alive",
        )
    finally:
        await shutdown(app)

    stopped = all(task.done() or task.cancelled() for task in app["workers"])
    check("workers shut down cleanly", stopped)

    failed = RESULTS.count(False)
    summary = "ALL PASS" if not failed else f"{failed} CHECK(S) FAILED"
    if WARNINGS:
        summary += f" ({len(WARNINGS)} warning(s))"
    print(f"== {summary} ==")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
