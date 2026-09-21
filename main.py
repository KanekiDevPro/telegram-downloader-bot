"""Bot entrypoint: wiring, worker startup, graceful shutdown.

Run:  python main.py          (polling — default)
      BOT_MODE=webhook python main.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from typing import Any, Optional

import redis.asyncio as aioredis
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import User

from core.config import CLOUD_API_UPLOAD_LIMIT_MB, Settings, get_settings, probe_url
from core.database import create_pool, init_db
from core.logging import setup_logging
from core.telegram_api import build_session, session_target
from handlers.admin import router as admin_router
from handlers.payment import router as payment_router
from handlers.user import router as user_router
from middlewares.user_middleware import UserMiddleware
from services import cobalt_cookies
from services.cobalt import CobaltService
from services.cookie_watch import CookieJarWatcher, run_cookie_watch
from services.doctor import http_reachable
from services.extractor import (
    BrowserSpecError,
    ExtractorService,
    parse_browser_spec,
    pot_plugin_installed,
    youtube_login_hint,
)
from services.helper_watch import run_helper_watch
from services.payments import build_payment_service
from services.queue import BLOCK_TIMEOUT_S, create_queue, create_redis_client
from services.worker import run_maintenance, run_worker

#: Startup probe of the PO-token provider: how many times, and how far apart.
#: Small on purpose — a provider that is really absent should not delay a boot.
POT_PROVIDER_PROBE_ATTEMPTS = 3
POT_PROVIDER_PROBE_DELAY_S = 1.5

logger = logging.getLogger(__name__)


async def connect_bot(settings: Settings) -> tuple[Bot, User]:
    """Return an authorized bot, degrading to the cloud API when the local one is down.

    A self-hosted Bot API server that is configured but unreachable — the
    ``telegram-api`` container exits immediately without ``TELEGRAM_API_ID`` and
    ``TELEGRAM_API_HASH`` — must not take the whole bot down: every queued
    download would be lost. We say so loudly and retarget to the official API
    instead, remembering that uploads are now capped at 50 MB.
    """
    bot = Bot(
        settings.bot_token,
        session=build_session(settings),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        return bot, await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        raise
    except Exception as exc:
        if not settings.uses_local_api:
            await bot.session.close()
            raise
        logger.error(
            "local Bot API server at %s is unreachable (%r) — falling back to the "
            "official cloud API: uploads larger than %s MB will be refused.",
            settings.telegram_api_base_url,
            exc,
            CLOUD_API_UPLOAD_LIMIT_MB,
        )
        logger.error(
            "start it with `docker compose --profile local-api up -d` after adding "
            "TELEGRAM_API_ID / TELEGRAM_API_HASH from https://my.telegram.org to .env"
        )
        await bot.session.close()
        settings.use_cloud_api_fallback()
        cloud_bot = Bot(
            settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        try:
            return cloud_bot, await cloud_bot.get_me()
        except Exception:
            await cloud_bot.session.close()
            raise


async def resolve_pot_provider(settings: Settings) -> str:
    """Return the PO-token provider URL only when the server actually answers.

    yt-dlp's bgutil plugin warns and skips the provider while it is down, which
    costs every download its token — the very thing the provider was meant to fix,
    and invisible unless someone reads the log. Dropping it here degrades to plain
    yt-dlp instead, visibly.

    Probed more than once on purpose: the provider now ships in the *default*
    stack, so one `docker compose up -d` starts the bot and the provider together,
    and a node server a moment behind (or a first run still pulling its image)
    would otherwise cost this whole process its tokens. A few tries, then the same
    graceful drop.

    The address returned is the one *this process* can reach
    (:func:`core.config.probe_url`), which is also the address yt-dlp needs: a
    service name resolves inside the compose network, and the published loopback
    port is what a host run has to use. Keeping the name on a host run would leave
    the provider configured and unreachable in the same breath.
    """
    url = settings.ytdlp_pot_provider_url
    if not url:
        return ""
    reachable = probe_url(url)
    for attempt in range(1, POT_PROVIDER_PROBE_ATTEMPTS + 1):
        if await http_reachable(reachable):
            return reachable
        if attempt < POT_PROVIDER_PROBE_ATTEMPTS:
            logger.info(
                "PO token provider at %s did not answer yet (%s/%s) — retrying",
                reachable,
                attempt,
                POT_PROVIDER_PROBE_ATTEMPTS,
            )
            await asyncio.sleep(POT_PROVIDER_PROBE_DELAY_S)
    logger.warning(
        "PO token provider at %s does not answer — continuing without it. "
        "Start it with `docker compose up -d pot-provider`, or clear "
        "YTDLP_POT_PROVIDER_URL.",
        reachable,
    )
    return ""


async def build_app(bot: Bot | None = None, *, send_digest: bool = True) -> dict[str, Any]:
    """Create every component and wire them together.

    ``bot`` is the authorized bot from :func:`connect_bot`; callers that only
    want an offline wiring check (``scripts/boot_check.py``) can omit it.
    ``send_digest=False`` keeps a diagnostic run from sending the weekly block
    report (and from stamping its window) on a live database.
    """
    settings = get_settings()

    pool = await create_pool()
    await init_db(pool)  # automated schema setup + plan seeding

    redis_client: Optional[aioredis.Redis] = None
    if settings.queue_backend == "redis":
        try:
            redis_client = create_redis_client(settings.redis_url)
            await redis_client.ping()
        except Exception:
            logger.warning(
                "Redis unavailable (%s) — falling back to in-memory queue & FSM state.",
                settings.redis_url,
            )
            redis_client = None

    # Redis also powers the FSM (user state machine) — as required by the spec.
    storage = RedisStorage(redis_client) if redis_client else MemoryStorage()

    if bot is None:
        bot = Bot(
            settings.bot_token,
            session=build_session(settings),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    dp = Dispatcher(storage=storage)

    dp["pool"] = pool
    dp["queue"] = create_queue(settings, redis_client)
    dp["payment_service"] = build_payment_service(pool)

    middleware = UserMiddleware()
    for r in (user_router, payment_router, admin_router):
        r.message.middleware(middleware)
        r.callback_query.middleware(middleware)
    dp.include_routers(user_router, payment_router, admin_router)

    pot_provider = await resolve_pot_provider(settings)
    extractor = ExtractorService(
        settings.download_dir,
        timeout_s=settings.extractor_timeout_s,
        download_timeout_s=settings.download_timeout_s,
        cookie_file=settings.cookie_file,
        proxy=settings.ytdlp_proxy,
        pot_provider_url=pot_provider,
        cookies_from_browser=settings.cookies_from_browser,
        js_runtime=settings.ytdlp_js_runtime,
        retry_attempts=settings.extractor_retry_attempts,
        retry_backoff_s=settings.extractor_retry_backoff_s,
    )
    # The admin /doctor command reads the extractor straight from the dispatcher.
    dp["extractor"] = extractor

    # The fallback engine: only engaged when yt-dlp comes back *blocked*, so a
    # configured instance costs nothing until the primary route is refused.
    # The address *this* process reaches the instance on: the compose service name
    # inside the network, the published loopback port for a bot run on the host.
    cobalt = CobaltService(
        probe_url(settings.cobalt_api_url),
        api_key=settings.cobalt_api_key,
        timeout_s=settings.cobalt_timeout_s,
        download_timeout_s=settings.cobalt_download_timeout_s,
        proxy=settings.cobalt_proxy or None,
    )
    dp["cobalt"] = cobalt
    if cobalt.enabled:
        logger.info(
            "fallback extractor: %s (used only when yt-dlp is blocked%s)",
            cobalt.base_url,
            ", API key set" if cobalt.api_key else "",
        )
    else:
        logger.info("fallback extractor: off (COBALT_API_URL empty)")

    logger.info("Telegram API target: %s", session_target(settings))
    logger.info("cookies: %s", settings.cookie_file if extractor.using_cookies else "none")
    if settings.cookie_file is not None and not extractor.using_cookies:
        # Logged here as well as at download time: a missing mount point or an
        # unreadable jar should be visible on the first screen of the log, not
        # discovered by a user whose link failed.
        extractor.warn_if_cookies_unusable()
    if hint := youtube_login_hint(settings.cookie_file):
        # An incomplete login is indistinguishable from an IP ban at download
        # time, so say it once at startup rather than letting it look random.
        logger.warning(hint)
    logger.info("yt-dlp proxy: %s", "configured" if extractor.using_proxy else "none")
    if extractor.js_runtime_name == "none":
        logger.warning(
            "no JavaScript runtime (node/deno/bun/qjs) found — YouTube extraction is "
            "degraded without one; set YTDLP_JS_RUNTIME or install a runtime."
        )
    else:
        logger.info("yt-dlp JavaScript runtime: %s", extractor.js_runtime_name)
    if extractor.using_pot_provider:
        if pot_plugin_installed():
            logger.info("PO token provider: %s", pot_provider)
        else:
            logger.warning(
                "YTDLP_POT_PROVIDER_URL is set but the bgutil plugin is not installed in "
                "this environment — the setting does nothing until bgutil-ytdlp-pot-provider "
                "is installed (it is in requirements.txt)."
            )
    elif settings.wants_pot_provider:
        # Configured but dropped above (or the plugin is missing, warned about just
        # above): say which, so "no provider" is never a mystery in the log.
        logger.info(
            "PO token provider: off — %s did not answer at startup; the bot keeps "
            "working without it and /doctor shows the live state.",
            probe_url(settings.ytdlp_pot_provider_url),
        )
    if settings.cookie_auto_export:
        # Validated here so a typo is a startup log line instead of a surprise the
        # first time a login block happens (the refresh itself still guards).
        try:
            parse_browser_spec(settings.cookie_auto_export)
        except BrowserSpecError as exc:
            logger.warning(
                "COOKIE_AUTO_EXPORT=%s is not a valid browser spec (%s) — automatic "
                "cookie refresh stays off until it is fixed.",
                settings.cookie_auto_export,
                exc,
            )
        else:
            logger.info(
                "automatic cookie refresh: %s after a login-shaped block (COOKIE_FILE must "
                "be writable — on a read-only mount, export on the host instead)",
                settings.cookie_auto_export,
            )
    if settings.cookies_from_browser and not extractor.using_browser_cookies:
        logger.warning(
            "COOKIES_FROM_BROWSER=%s was ignored (see the warning above); "
            "scripts/export_cookies.py writes a cookies.txt instead.",
            settings.cookies_from_browser,
        )
    if settings.max_file_size_mb > CLOUD_API_UPLOAD_LIMIT_MB and not settings.uses_local_api:
        logger.warning(
            "MAX_FILE_SIZE_MB=%s but no local Bot API server is configured: the cloud "
            "API only accepts bot uploads up to %s MB, so the limit is capped there. "
            "Set TELEGRAM_API_BASE_URL to a telegram-bot-api instance (see docker-compose).",
            settings.max_file_size_mb,
            CLOUD_API_UPLOAD_LIMIT_MB,
        )

    stop_event = asyncio.Event()
    workers = [
        asyncio.create_task(
            run_worker(i, stop_event, bot, pool, dp["queue"], extractor, cobalt),
            name=f"worker-{i}",
        )
        for i in range(settings.worker_count)
    ]
    # Maintenance always runs: premium expiry must not depend on ADMIN_IDS being set.
    # It also carries the weekly block digest (silent when nothing failed).
    workers.append(
        asyncio.create_task(
            run_maintenance(
                stop_event,
                pool,
                bot,
                settings.admin_ids if send_digest else (),
            ),
            name="maintenance",
        )
    )

    # The fallback engine reads its own cookie file, in its own shape — generated
    # here from the jar the bot already has, so one export signs both engines in.
    # Cobalt reads it at *startup*, which is why this runs before anything asks it
    # for a link, and why a later change needs a cobalt restart (says so in
    # /doctor and in the jar alert).
    # The directory is checked *before* the sync so a permission problem is one
    # clear error line at boot instead of a traceback three steps into startup.
    # It is a Docker bind-mount trap: the host's ./cobalt keeps its own owner, so
    # the mode set in the image does not apply to what is actually mounted there.
    if directory_problem := cobalt_cookies.ensure_cookie_dir(settings.cobalt_cookies_dir):
        logger.error("cobalt cookies: %s", directory_problem)
    cobalt_cookie_state = cobalt_cookies.sync_from_jar(settings, jar_path=extractor.cookie_file)
    if cobalt_cookie_state.off:
        logger.info("cobalt cookies: not generated (COBALT_COOKIES_DIR is empty)")
    elif cobalt_cookie_state.usable and cobalt_cookie_state.written:
        logger.info(
            "cobalt cookies: %s (%s) — cobalt needs a restart to pick this up",
            cobalt_cookie_state.describe(),
            cobalt_cookie_state.source or "jar",
        )
    else:
        logger.info("cobalt cookies: %s", cobalt_cookie_state.describe())

    # A replaced cookie jar is picked up by the next download — silently. This
    # watcher tells the admins when one is waiting, instead of waiting for /doctor.
    cookie_watch = CookieJarWatcher(extractor, interval_s=settings.cookie_watch_interval_s)
    if cookie_watch.enabled:
        # The jar present at startup is the baseline: only an export made while
        # the bot *runs* is news, so restarts stay quiet.
        jar = cookie_watch.prime()
        logger.info(
            "cookie-jar watcher: every %.0fs (%s, in sync: %s) — an export the running "
            "bot has not loaded alerts the admins",
            cookie_watch.interval_s,
            jar.kind,
            jar.in_sync,
        )
        if settings.admin_ids:
            workers.append(
                asyncio.create_task(
                    run_cookie_watch(
                        stop_event,
                        bot,
                        settings.admin_ids,
                        cookie_watch,
                        pool,
                        settings=settings,
                    ),
                    name="cookie-watch",
                )
            )
        else:
            logger.warning(
                "cookie-jar watcher has nobody to alert — set ADMIN_IDS, or turn it off "
                "with COOKIE_WATCH_INTERVAL_S=0."
            )

    # The two YouTube helpers are part of the stack now, so their health is watched
    # on a timer as well: a helper that dies at night becomes a recorded outage with
    # one page, instead of the next user's failed link being the first news of it.
    # It writes nothing while a state holds, and needs no admin to be useful.
    if settings.helper_watch_interval_s > 0:
        if not settings.admin_ids:
            logger.warning(
                "helper watch has nobody to alert — set ADMIN_IDS, or turn it off "
                "with HELPER_WATCH_INTERVAL_S=0."
            )
        workers.append(
            asyncio.create_task(
                run_helper_watch(
                    stop_event,
                    pool,
                    bot,
                    settings.admin_ids,
                    settings=settings,
                ),
                name="helper-watch",
            )
        )
    else:
        logger.info("helper watch: off (HELPER_WATCH_INTERVAL_S=0)")

    return {
        "bot": bot,
        "dp": dp,
        "pool": pool,
        "queue": dp["queue"],
        "extractor": extractor,
        "cookie_watch": cookie_watch,
        "cobalt_cookies": cobalt_cookie_state,
        "redis": redis_client,
        "cobalt": cobalt,
        "workers": workers,
        "stop_event": stop_event,
    }


async def shutdown(app: dict[str, Any]) -> None:
    """Graceful teardown: let workers drain briefly, then cancel and close clients."""
    app["stop_event"].set()
    tasks = app["workers"]
    if tasks:
        # Workers notice the stop event at the next queue read (BLOCK_TIMEOUT_S),
        # so this only needs to cover that plus one task in flight. Fall back to
        # cancellation if a worker is stuck in a long download instead.
        _, pending = await asyncio.wait(tasks, timeout=BLOCK_TIMEOUT_S + 3)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    with suppress(Exception):
        await app["pool"].close()
    redis = app.get("redis")
    if redis is not None:
        with suppress(Exception):
            await redis.aclose()
    with suppress(Exception):
        await app["bot"].session.close()
    cobalt = app.get("cobalt")
    if cobalt is not None:
        with suppress(Exception):
            await cobalt.close()
    logger.info("shutdown complete")


async def run_polling(app: dict[str, Any]) -> None:
    bot: Bot = app["bot"]
    dp: Dispatcher = app["dp"]
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        app["stop_event"].set()
        asyncio.create_task(dp.stop_polling())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows / non-main-thread fallbacks rely on KeyboardInterrupt

    # Drop any webhook left over from a previous deployment, otherwise polling
    # and the webhook fight over the same updates.
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await shutdown(app)


async def run_webhook(app: dict[str, Any]) -> None:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    settings = get_settings()
    bot: Bot = app["bot"]
    dp: Dispatcher = app["dp"]

    web_app = web.Application()
    handler = SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=settings.webhook_secret or None
    )
    handler.register(web_app, path=settings.webhook_path)
    setup_application(web_app, dp, bot=bot)

    webhook_url = f"{settings.webhook_url.rstrip('/')}{settings.webhook_path}"
    await bot.set_webhook(webhook_url, secret_token=settings.webhook_secret or None)
    logger.info("webhook set: %s", webhook_url)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.webhook_host, settings.webhook_port)
    await site.start()
    logger.info("listening on %s:%s%s", settings.webhook_host, settings.webhook_port, settings.webhook_path)

    try:
        await app["stop_event"].wait()
    finally:
        await runner.cleanup()
        with suppress(Exception):
            await bot.delete_webhook()
        await shutdown(app)


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    if not settings.bot_token:
        raise SystemExit(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill in your token."
        )
    if not settings.admin_ids:
        logger.warning("ADMIN_IDS is empty — payment receipts will have nobody to forward to.")
    if not settings.manual_card_number or not settings.manual_card_holder:
        logger.warning(
            "MANUAL_CARD_NUMBER / MANUAL_CARD_HOLDER are not set — the card-to-card "
            "checkout screen has no card to show, so nobody can buy premium."
        )

    # Authorize before touching the database: an unreachable API server should
    # fail fast instead of leaving a half-started stack behind.
    try:
        bot, me = await connect_bot(settings)
    except TelegramUnauthorizedError as exc:
        raise SystemExit("BOT_TOKEN was rejected by Telegram — check .env.") from exc
    except Exception as exc:
        target = settings.telegram_api_base_url or "api.telegram.org"
        raise SystemExit(
            f"Could not reach the Telegram API at {target}: {exc!r}\n"
            "  - running the bot outside Docker? Point TELEGRAM_API_BASE_URL at\n"
            "    http://localhost:8081 instead of the in-network service name.\n"
            "  - want the official cloud API? Clear TELEGRAM_API_BASE_URL in .env."
        ) from exc
    logger.info("authorized as @%s (id=%s)", me.username or "?", me.id)
    logger.info("upload target: %s (max %s MB)", session_target(settings), settings.upload_limit_mb)

    app = await build_app(bot)

    if settings.bot_mode == "webhook":
        await run_webhook(app)
    else:
        await run_polling(app)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
