"""Integration smoke test — requires live Postgres + Redis (docker-compose.yml).

Usage:  python scripts/smoke.py

Covers: schema bootstrap, plan seeding, user upsert, quota claiming,
transaction lifecycle (manual strategy), smart cache roundtrip, Redis queue
roundtrip, yt-dlp support probing, and a live metadata extraction (network
dependent — reported as a warning, not a failure, when it can't run).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import uuid
from dataclasses import replace

from core import database
from core.config import get_settings, probe_url
from core.logging import force_utf8_console
from core.utils import format_size, today_local, utcnow
from services import cache as cache_service
from services import cobalt_cookies, fallback, preflight, telemetry
from services.cobalt import CobaltService, url_is_fetchable
from services.cookie_watch import CookieJarWatcher
from services.delivery import join_file_ids, split_file_ids
from services.doctor import fallback_health as doctor_fallback_health
from services.extractor import RETRYABLE_EXTRACTION_CODES, ExtractionError, ExtractorService
from services.payments.manual import ManualPaymentStrategy
from services.queue import (
    BLOCK_TIMEOUT_S,
    DownloadTask,
    MemoryTaskQueue,
    RedisTaskQueue,
    create_redis_client,
)

TEST_USER_A = 4_242_420  # subscription flow
TEST_USER_B = 4_242_421  # quota flow
WARNINGS: list[str] = []


class _NoBot:
    """A bot that fails the run if it is asked to send (the gate must hold first)."""

    async def send_message(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("the weekly gate let a digest through")


class _RecordingBot:
    """Collects what would be sent, so a live-database check sends nothing."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.messages.append(text)


def verdict(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✔' if ok else '✘'} {name}" + (f" — {detail}" if detail else ""))
    return ok


async def main() -> int:
    force_utf8_console()  # ✔/✘ and Persian text crash a legacy Windows console
    settings = get_settings()
    print(f"== smoke: {settings.database_url.split('@')[-1]} / queue={settings.queue_backend} ==")
    ok = True

    # --- database bootstrap -------------------------------------------------
    pool = await database.create_pool()
    await database.init_db(pool)
    tables = {
        r[0]
        for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    required = {
        "users",
        "subscription_plans",
        "transactions",
        "smart_cache",
        "block_events",
        "bot_state",
    }
    ok &= verdict("schema tables created", required <= tables, str(required & tables))

    plan_count = await pool.fetchval("SELECT COUNT(*) FROM subscription_plans")
    ok &= verdict("subscription plans seeded", int(plan_count) >= 3, f"count={plan_count}")

    # --- user upsert --------------------------------------------------------
    row = await database.get_or_create_user(pool, TEST_USER_A, "smoketest")
    ok &= verdict("user upsert (insert)", row["username"] == "smoketest")
    row2 = await database.get_or_create_user(pool, TEST_USER_A, None)
    ok &= verdict("user upsert (keep old username)", row2["username"] == "smoketest")

    # --- daily quota claim --------------------------------------------------
    await database.get_or_create_user(pool, TEST_USER_B, "quotauser")
    c1 = await database.can_claim_download(pool, TEST_USER_B, 2, today_local())
    c2 = await database.can_claim_download(pool, TEST_USER_B, 2, today_local())
    c3 = await database.can_claim_download(pool, TEST_USER_B, 2, today_local())
    usage = await database.get_daily_usage(pool, TEST_USER_B)
    assert usage is not None  # the upsert above guarantees the row
    ok &= verdict(
        "quota claim (2 allowed, 3rd blocked)",
        c1 and c2 and not c3,
        f"used={usage['daily_downloads']}",
    )

    # --- support contact + broadcast paging ---------------------------------
    # Both are on paths a user reaches (the menu's support button, and the panel's
    # broadcast): one row in ``bot_state``, and a query that has to walk the users
    # table without ever repeating or skipping an id.
    await database.set_support_contact(pool, "@smoke_support")
    stored_contact = await database.get_support_contact(pool)
    ok &= verdict(
        "support contact round-trips through bot_state",
        stored_contact == "@smoke_support",
        stored_contact or "(empty)",
    )
    first_page = await database.user_id_page(pool, limit=1)
    second_page = await database.user_id_page(pool, after=first_page[-1] if first_page else 0, limit=1)
    ok &= verdict(
        "broadcast pages move forward without repeating an id",
        bool(first_page) and all(value not in second_page for value in first_page),
        f"first={first_page} second={second_page}",
    )
    total_users = await database.count_users(pool)
    ok &= verdict("the broadcast count sees the users above", total_users >= 2, f"users={total_users}")
    await database.set_support_contact(pool, "")

    # --- transaction lifecycle (manual strategy) ----------------------------
    strategy = ManualPaymentStrategy(pool)
    plans = await database.list_plans(pool)
    plan = plans[0]
    txn = await strategy.begin(row, plan)
    ok &= verdict("transaction begin (pending)", txn["status"] == "pending" and txn["method"] == "manual")

    attached = await strategy.attach_receipt(txn["id"], "AgAC_TEST_RECEIPT_PHOTO_ID")
    ok &= verdict("receipt attach", attached)
    stored = await database.get_transaction(pool, txn["id"])
    assert stored is not None
    ok &= verdict("receipt persisted", stored["receipt_photo_id"] == "AgAC_TEST_RECEIPT_PHOTO_ID")

    outcome = await strategy.decide(txn["id"], approved=True)
    user_after = await database.get_user(pool, TEST_USER_A)
    assert user_after is not None
    ok &= verdict(
        "approve → premium granted + expiry set",
        outcome is not None
        and outcome["status"] == "approved"
        and user_after["is_premium"]
        and user_after["premium_until"] is not None,
        f"until={user_after['premium_until']}",
    )
    again = await strategy.decide(txn["id"], approved=True)
    ok &= verdict("double-approve blocked", again is None)

    txn2 = await strategy.begin(row, plans[1])
    await strategy.attach_receipt(txn2["id"], "AgAC_TEST_RECEIPT_2")
    outcome2 = await strategy.decide(txn2["id"], approved=False)
    ok &= verdict("reject flow", outcome2 is not None and outcome2["status"] == "rejected")

    # --- smart cache --------------------------------------------------------
    url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ&utm_source=test&utm_medium=bot"
    key = cache_service.cache_key(url, "video")
    await cache_service.memorize(
        pool,
        url=url,
        platform="youtube",
        telegram_file_id="AgAC_TEST_FILE_ID",
        request=cache_service.request_key("video"),
    )
    hit = await cache_service.get_cached(pool, url, "video")
    same_key = cache_service.cache_key("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "video")
    ok &= verdict("cache store + canonical-key hit", hit is not None and key == same_key)
    audio_miss = await cache_service.get_cached(pool, url, "audio")
    ok &= verdict("cache is per format (audio misses the video entry)", audio_miss is None)
    # A quality *tier* is part of the key too: a 480p ask must not replay the 1080p
    # file (and the default tier keeps the key older rows already own).
    tier_miss = await cache_service.get_cached(pool, url, "video", "480")
    ok &= verdict("cache is per quality tier (480p misses the default entry)", tier_miss is None)
    await cache_service.forget(pool, url, "video")
    gone = await cache_service.get_cached(pool, url, "video")
    ok &= verdict("cache forget", gone is None)

    # A photo album is several file_ids in one column, and ``kind`` is what tells the
    # delivery layer to send them as a media group again rather than as one document.
    album_url = "https://x.com/user/status/12345"
    await cache_service.memorize(
        pool,
        url=album_url,
        platform="twitter",
        telegram_file_id=join_file_ids(["AgAC_PHOTO_1", "AgAC_PHOTO_2"]),
        request=cache_service.request_key("video"),
        kind="photo_group",
    )
    album = await cache_service.get_cached(pool, album_url, "video")
    ok &= verdict(
        "a cached album keeps every id and its kind",
        album is not None
        and album["kind"] == "photo_group"
        and split_file_ids(album["telegram_file_id"]) == ["AgAC_PHOTO_1", "AgAC_PHOTO_2"]
        and cache_service.cache_key(album_url, "video") == album["url_hash"],
        "no row" if album is None else str(album["kind"]),
    )
    await cache_service.forget(pool, album_url, "video")

    # --- queue roundtrip ----------------------------------------------------
    r = create_redis_client(settings.redis_url)
    await r.ping()
    rq = RedisTaskQueue(r, "smoke:dl:tasks")
    task = DownloadTask(
        url="https://youtu.be/abc123",
        telegram_id=TEST_USER_A,
        chat_id=TEST_USER_A,
        media_format="audio",
        url_hash="h" * 64,
    )
    depth = await rq.enqueue(task)
    popped = await rq.dequeue()
    ok &= verdict(
        "redis queue roundtrip",
        popped == task and depth == 1,
        f"depth={depth}, media={popped.media_format if popped else None}",
    )
    depth_again = await rq.requeue(task)
    back = await rq.dequeue()
    ok &= verdict("redis requeue roundtrip", back == task and depth_again == 1)

    # An idle read must return None rather than raise: redis-py's 5s connection
    # default used to abort a 30s BRPOP and kill the worker outright.
    started = time.monotonic()
    idle = await rq.dequeue()
    elapsed = time.monotonic() - started
    ok &= verdict(
        "redis idle read returns None (no socket-timeout abort)",
        idle is None and elapsed >= BLOCK_TIMEOUT_S - 1,
        f"after {elapsed:.1f}s",
    )

    mq = MemoryTaskQueue()
    await mq.enqueue(task)
    popped_m = await mq.dequeue()
    ok &= verdict("memory queue roundtrip", popped_m == task)
    await mq.requeue(task)
    ok &= verdict("memory requeue roundtrip", await mq.dequeue() == task)

    # --- yt-dlp support probing (no network) --------------------------------
    ok &= verdict("probe: youtube supported", ExtractorService.is_url_supported("https://www.youtube.com/watch?v=aqz-KE-bpKQ"))
    ok &= verdict("probe: tiktok supported", ExtractorService.is_url_supported("https://www.tiktok.com/@user/video/1234567890"))
    ok &= verdict("probe: garbage rejected", not ExtractorService.is_url_supported("https://definitely-not-real-xyz-123.example/video"))

    # --- retry policy (offline) ---------------------------------------------
    policy = ExtractorService(
        settings.download_dir,
        js_runtime="none",
        retry_attempts=settings.extractor_retry_attempts,
        retry_backoff_s=settings.extractor_retry_backoff_s,
    )
    ok &= verdict(
        "only stale sessions are retried",
        RETRYABLE_EXTRACTION_CODES == frozenset({"SESSION_STALE"}),
        f"attempts={policy.retry_attempts}, backoff={policy.retry_backoff_s}s",
    )

    # --- mounted cookie jar (offline) ---------------------------------------
    # yt-dlp rewrites its cookiefile when a download ends, so the jar is mounted
    # read-only and copied to a writable path first; without the copy every
    # download would fail at the last step on a deployed (mounted) jar.
    cookie_jar = settings.cookie_file
    if cookie_jar is not None and cookie_jar.is_file():
        mounted = ExtractorService(
            settings.download_dir, js_runtime="none", cookie_file=cookie_jar
        )
        handed_raw = mounted._base_opts(extract_only=True).get("cookiefile")
        handed = Path(handed_raw) if isinstance(handed_raw, str) else None
        ok &= verdict(
            "cookie jar is handed over as a writable copy (mount stays read-only)",
            handed is not None and handed != cookie_jar and os.access(handed, os.W_OK),
            f"{cookie_jar} -> {handed}",
        )

        # A fresh export takes effect on the next download without a word; the
        # watcher is the only thing that tells an admin it is still waiting.
        watcher = CookieJarWatcher(mounted, interval_s=settings.cookie_watch_interval_s)
        baseline = watcher.prime()
        if baseline.kind == "ok":
            ok &= verdict(
                "cookie-jar watcher stays quiet for the jar it started with",
                watcher.inspect(baseline) is None,
                f"every {watcher.interval_s:.0f}s, {baseline.cookie_count} cookies",
            )
            # An export made while the bot runs: same shape, newer bytes, not loaded.
            exported = replace(
                baseline, exported_at=(baseline.exported_at or 0.0) + 1.0, in_sync=False
            )
            first = watcher.inspect(exported)
            ok &= verdict(
                "a waiting export is announced once, then not again",
                first is not None and watcher.inspect(exported) is None,
                "one alert per export",
            )
        else:
            print(f"  ⚠ jar is {baseline.kind} — skipping the watcher decision checks")
    else:
        print("  ⚠ no cookie jar on disk — skipping the writable-copy check")

    # --- live metadata extraction (network dependent) -----------------------
    extractor = ExtractorService(
        settings.download_dir,
        timeout_s=45,
        cookie_file=settings.cookie_file,
        proxy=settings.ytdlp_proxy,
        # The same clients and address family the bot uses, so this probe answers
        # about the bot's path rather than about yt-dlp's defaults.
        youtube_clients=settings.ytdlp_youtube_clients,
        force_ipv4=settings.ytdlp_force_ipv4,
        # The same EJS script sources, so a solved n challenge here means one on
        # the bot (and a "n challenge solving failed" here names the same fix).
        remote_components=settings.ytdlp_remote_components,
        # One attempt only: this probe is a diagnosis, not a download.
        retry_attempts=0,
    )
    try:
        info = await extractor.extract("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
        print(
            f"  ℹ live extract: '{info.title[:50]}' | {info.platform} | "
            f"{format_size(info.filesize_approx)} | ext={info.extension}"
        )
        ok &= verdict("live extract (youtube)", True)
    except Exception as exc:  # noqa: BLE001 — network/site dependent by design
        WARNINGS.append(f"live extract skipped/failed: {exc!r}")
        print(f"  ⚠ live extract (youtube): {exc!r}")

    # --- the fallback engine ------------------------------------------------
    # yt-dlp is the primary engine; when the *site* refuses this host, another
    # address may not be refused. What is checked here is the wiring and the
    # decision (both cheap, both local); the instance itself is probed live and
    # only warned about, because an unreachable fallback degrades to exactly the
    # behaviour that existed before it — it must not fail the deployment.
    # The same translation the bot makes: this script runs on the host, where a
    # compose service name does not resolve and the instance is on its published
    # loopback port.
    cobalt = CobaltService(
        [probe_url(url) for url in settings.cobalt_endpoints],
        api_key=settings.cobalt_api_key,
        timeout_s=settings.cobalt_timeout_s,
    )
    ok &= verdict(
        "the fallback engine is configured",
        cobalt.enabled == settings.cobalt_enabled,
        f"{cobalt.pool_label() or '(off)'}" + (" +key" if settings.cobalt_api_key else ""),
    )
    ok &= verdict(
        "a host YouTube has flagged has more than one address to fall back to",
        len(cobalt.base_urls) >= 2,
        " | ".join(cobalt.base_urls),
    )
    blocked = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    geo = ExtractionError("GEO_RESTRICTED", "geo")
    ok &= verdict(
        "only a refusal the net can answer is handed to the fallback",
        fallback.should_use_fallback(blocked, cobalt)
        and fallback.should_use_fallback(ExtractionError("SESSION_STALE", "stale"), cobalt)
        and fallback.should_use_fallback(ExtractionError("DRM_PROTECTED", "drm"), cobalt)
        # An image post: no video here, pictures there.
        and fallback.should_use_fallback(ExtractionError("IMAGE_ONLY", "no video"), cobalt)
        and not fallback.should_use_fallback(geo, cobalt)
        and not fallback.should_use_fallback(blocked, None),
        "blocked/stale/drm/image-post yes — private/geo/live no",
    )
    # The preflight holds a YouTube link back only when a refusal was *observed*;
    # a fallback that may still serve it turns even that into a note.
    youtube_link = "https://youtu.be/aqz-KE-bpKQ"
    preflight.note_anonymous_refusal()
    try:
        refused_alone = preflight.youtube_preflight(youtube_link, settings.cookie_file)
        # Rendered in Persian: the wording asserted below is the Persian one
        # (the product's default is English, so the language must be pinned here).
        refused_with_fallback = preflight.youtube_preflight(
            youtube_link, settings.cookie_file, fallback_available=True, lang="fa"
        )
    finally:
        preflight.clear_anonymous_refusal()
    if refused_alone.refused:
        ok &= verdict(
            "a link held back without the fallback is queued with it",
            not refused_with_fallback.refused and "جایگزین" in refused_with_fallback.message,
            refused_with_fallback.message.split("\n")[0],
        )
    else:
        print(
            "  note  the jar signs in, so the preflight had nothing to refuse — "
            f"with the fallback the verdict is '{refused_with_fallback.status}'"
        )
    if cobalt.enabled:
        # A link the instance serves with no session of its own, so a zero-config
        # stack gets a real answer here — and the *shape* of that answer is the
        # check: the resolved URL is the fallback's whole deliverable, and services
        # (streamable, protocol-relative CDNs) do hand out malformed ones. Strict
        # on purpose — a repaired link passes, a link nobody repaired fails here
        # instead of inside a user's transfer.
        # The fallback's own login: the same jar, written into cobalt's file. Its
        # *shape* is pinned here because getting it wrong is silent — the file
        # loads and YouTube still answers `error.api.youtube.login` — and so is
        # whether the running instance has the version that is on disk (it reads
        # the file once, at startup).
        cookie_state = cobalt_cookies.sync_from_jar(settings)
        if cookie_state.off:
            print("  note  cobalt cookies: off (COBALT_COOKIES_DIR is empty)")
        else:
            document: object = {}
            try:
                document = json.loads(Path(cookie_state.path).read_text(encoding="utf-8"))  # type: ignore[arg-type]
            except (OSError, ValueError):
                document = {}
            entries = document.get("youtube") if isinstance(document, dict) else None
            ok &= verdict(
                "the fallback's cookie file is the shape cobalt parses",
                cookie_state.usable
                and isinstance(entries, list)
                and all(isinstance(entry, str) for entry in entries),
                f"{cookie_state.cookie_count} cookie(s) → {cookie_state.path}",
            )
            started_at = await cobalt.server_start_time()
            if cobalt_cookies.restart_needed(cookie_state.generated_at, started_at) is True:
                print(
                    "  note  cobalt is running an older cookie file — "
                    "docker compose restart cobalt loads this one"
                )
        try:
            started = time.monotonic()
            served = await cobalt.resolve("https://streamable.com/moo", "video")
            ok &= verdict(
                "the fallback instance resolves a link it can serve on its own",
                url_is_fetchable(served.url),
                f"in {time.monotonic() - started:.1f}s → {served.url.split('?')[0][:60]}",
            )
        except Exception as exc:  # noqa: BLE001 — network/instance dependent by design
            WARNINGS.append(f"fallback probe failed: {fallback.describe(exc)}")
            print(f"  ⚠ fallback probe: {fallback.describe(exc)}")
        try:
            started = time.monotonic()
            media = await cobalt.resolve("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "video")
            ok &= verdict(
                "the fallback instance resolves a real link",
                media.url.startswith(("http://", "https://")),
                f"in {time.monotonic() - started:.1f}s → {media.url.split('?')[0][:60]}",
            )
        except Exception as exc:  # noqa: BLE001 — network/instance dependent by design
            WARNINGS.append(f"fallback probe failed: {fallback.describe(exc)}")
            print(f"  ⚠ fallback probe: {fallback.describe(exc)}")
        finally:
            await cobalt.close()

    # --- the net's health, as an admin reads it (live DB) -------------------
    # /blocks answers "which instance, does it answer, and what happened the last
    # time a blocked link needed it" without spending a request — from a quarantine
    # in this process plus the row the worker writes on real traffic. This writes a
    # synthetic row, reads it back through the very same function both commands use,
    # and then puts the row back exactly as it was: a *test* must not erase what the
    # deployed bot recorded about the net.
    previous_use = await database.get_state(pool, fallback.USE_STATE_KEY)
    await fallback.remember_use(
        pool, fallback.USE_SKIPPED, "قرنطینه بود (ERROR: instance is down)"
    )
    health = await doctor_fallback_health(settings, cobalt, probe=False, pool=pool)
    net_line = health.line()
    ok &= verdict(
        "the recorded use reaches the /blocks footer",
        "⏭ رد شد (استفاده نشد)" in net_line
        and "قرنطینه بود (ERROR: instance is down)" in net_line
        and "همین حالا" in net_line,
        next(
            (line.strip() for line in net_line.splitlines() if "آخرین لینک بلاک‌شده" in line),
            "(missing)",
        ),
    )
    # The address the line must name is the one this run actually talks to: on the
    # host that is the published loopback port, not the compose name in `.env`.
    expected_url = cobalt.base_url if cobalt.enabled else ""
    ok &= verdict(
        "the footer names the instance and its state",
        expected_url in net_line if expected_url else "⚫️ خاموش" in net_line,
        net_line.splitlines()[0][:120],
    )
    if previous_use is None:
        await pool.execute("DELETE FROM bot_state WHERE key = $1", fallback.USE_STATE_KEY)
    else:
        await database.set_state(pool, fallback.USE_STATE_KEY, previous_use)

    # --- block telemetry (live DB) ------------------------------------------
    # Every failed download is stored with its cause; the weekly digest turns those
    # rows into "one cookie re-export" or "time for a proxy".
    marker = f"smoke-{uuid.uuid4().hex[:8]}.example"
    # One repeatable-read transaction on one connection, and the window taken from
    # the *database's* clock: `created_at` is set server-side, so a host-side
    # `utcnow()` races it by fractions of a millisecond (and loses the first row
    # when it does) — while the snapshot keeps a live user's block, landed in the
    # same seconds, from being counted as this run's.
    async with pool.acquire() as connection:
        async with connection.transaction(isolation="repeatable_read"):
            window_start = await connection.fetchval("SELECT now()")
            for cause in ("login", "login", "ip", "site"):
                # A connection, not the pool: the counts below must read the same
                # snapshot this transaction writes. (asyncpg types that surface the
                # same way, so this needs no cast.)
                await database.record_block_event(
                    connection,
                    telegram_id=TEST_USER_B,
                    url_host=marker,
                    code="EXTRACTOR_BLOCKED" if cause != "site" else "PRIVATE_VIDEO",
                    cause=cause,
                )
            # `now()` is the *transaction's* timestamp, which is exactly what the
            # inserted rows carry — so it is the tight lower bound. The upper one
            # has to move, hence `clock_timestamp()`: with `now()` again the window
            # would be zero-width and every row would fall out of it.
            window_end = await connection.fetchval("SELECT clock_timestamp()")
            counts = await database.block_counts(connection, window_start, window_end)
            top = await database.top_block_host(connection, window_start, window_end)
    ok &= verdict(
        "a failed download is recorded with its cause",
        counts == {"login": 2, "ip": 1, "site": 1},
        str(counts),
    )
    ok &= verdict("the noisiest host is identified", top == (marker, 4), str(top))

    digest = await telemetry.build_digest(pool, days=7)
    report = telemetry.render_digest(digest, headline=f"گزارش {telemetry.DIGEST_DAYS} روزهٔ شکست‌ها")
    ok &= verdict(
        "the digest reads the stored causes",
        "کوکی/لاگین" in report and "قدم بعدی" in report,
        f"{digest.total} failure(s) in {digest.window}",
    )
    ok &= verdict(
        "the digest's next step follows the dominant cause",
        "اکسپورت تازه" in telemetry.next_step(digest) if digest.count("login") else True,
        telemetry.next_step(digest),
    )

    await database.set_state(pool, "smoke_state", "1")
    ok &= verdict("bot_state remembers a value", await database.get_state(pool, "smoke_state") == "1")
    ok &= verdict(
        "an unknown key reads as None",
        await database.get_state(pool, "smoke_state_missing") is None,
    )

    # Old rows must not accumulate forever: the digest reads a week, the table
    # keeps a quarter.
    await pool.execute(
        """
        INSERT INTO block_events (created_at, telegram_id, url_host, code, cause)
        VALUES (now() - make_interval(days => $1), $2, $3, 'GENERAL', 'site')
        """,
        30,
        TEST_USER_B,
        marker,
    )
    pruned = await database.prune_block_events(pool, keep_days=7)
    ok &= verdict("telemetry older than the window is pruned", pruned >= 1, f"removed={pruned}")
    still_there = await pool.fetchval(
        "SELECT COUNT(*) FROM block_events WHERE url_host = $1", marker
    )
    ok &= verdict("the window itself survives the prune", int(still_there) == 4, f"rows={still_there}")

    # The weekly gate: silence is the healthy state, and a stamp means "delivered".
    # The previous stamp is put back afterwards — a *test* must not change when the
    # real digest is due.
    previous_stamp = await database.get_state(pool, telemetry.DIGEST_STATE_KEY)
    await database.set_state(pool, telemetry.DIGEST_STATE_KEY, utcnow().isoformat())
    ok &= verdict(
        "a digest already sent this week is not sent again",
        await telemetry.maybe_send_digest(pool, _NoBot(), settings.admin_ids) is False,  # type: ignore[arg-type]
        f"days={telemetry.DIGEST_DAYS}",
    )
    if previous_stamp is None:
        await pool.execute("DELETE FROM bot_state WHERE key = $1", telemetry.DIGEST_STATE_KEY)
    else:
        await database.set_state(pool, telemetry.DIGEST_STATE_KEY, previous_stamp)

    # The alarm: a *run* of login failures pages the admins in minutes instead of
    # a week. Counts are global and this may be a live database, so the threshold
    # is placed two failures above whatever is already in the hour instead of
    # assuming a clean one — and the stamp is saved and put back for the same
    # reason as the digest's (a test must not silence the real alarm).
    if not settings.admin_ids:
        print("  ⚠ no ADMIN_IDS — skipping the early-alert checks")
    else:
        previous_alert_stamp = await database.get_state(pool, telemetry.EARLY_ALERT_STATE_KEY)
        await pool.execute("DELETE FROM bot_state WHERE key = $1", telemetry.EARLY_ALERT_STATE_KEY)
        alert_marker = f"smoke-alert-{uuid.uuid4().hex[:8]}.example"
        recording = _RecordingBot()
        logins_in_hour = (await telemetry.build_recent_digest(pool)).count("login")
        threshold = logins_in_hour + 2
        ok &= verdict(
            "one login failure is not yet a pattern",
            await telemetry.maybe_send_early_alert(
                pool, recording, settings.admin_ids, threshold=threshold  # type: ignore[arg-type]
            )
            is False,
            f"logins in the hour: {logins_in_hour}, threshold: {threshold}",
        )
        for _ in range(2):
            await database.record_block_event(
                pool,
                telegram_id=TEST_USER_B,
                url_host=alert_marker,
                code="EXTRACTOR_BLOCKED",
                cause="login",
            )
        paged = await telemetry.maybe_send_early_alert(
            pool, recording, settings.admin_ids, threshold=threshold  # type: ignore[arg-type]
        )
        ok &= verdict(
            "a run of login failures pages the admins now",
            paged
            and len(recording.messages) == len(settings.admin_ids)
            and "🚨" in recording.messages[0],
            f"{len(recording.messages)} message(s) → {len(settings.admin_ids)} admin(s)",
        )
        ok &= verdict(
            "the alarm keeps its own cooldown",
            await telemetry.maybe_send_early_alert(
                pool, recording, settings.admin_ids, threshold=threshold  # type: ignore[arg-type]
            )
            is False
            and len(recording.messages) == len(settings.admin_ids),
            "one page per half hour",
        )
        if previous_alert_stamp is None:
            await pool.execute("DELETE FROM bot_state WHERE key = $1", telemetry.EARLY_ALERT_STATE_KEY)
        else:
            await database.set_state(pool, telemetry.EARLY_ALERT_STATE_KEY, previous_alert_stamp)

    # --- fixes and the trend (live DB) ---------------------------------------
    # /trend weighs the failures against a *recorded* fix, so both halves are
    # exercised here on the real database: the rows are backdated (a trend needs
    # days on either side of the fix) and removed again — a smoke run must not
    # leave a fabricated fix in production.
    fix_detail = f"smoke {uuid.uuid4().hex[:8]}"
    trend_marker = f"smoke-trend-{uuid.uuid4().hex[:8]}.example"
    for cause, days_ago in (("login", 3), ("login", 3), ("login", 3), ("login", 3), ("ip", 3)):
        await pool.execute(
            """
            INSERT INTO block_events (created_at, telegram_id, url_host, code, cause)
            VALUES (now() - make_interval(days => $1), $2, $3, 'EXTRACTOR_BLOCKED', $4)
            """,
            days_ago,
            TEST_USER_B,
            trend_marker,
            cause,
        )
    await pool.execute(
        """
        INSERT INTO block_events (created_at, telegram_id, url_host, code, cause)
        VALUES (now(), $1, $2, 'EXTRACTOR_BLOCKED', 'login')
        """,
        TEST_USER_B,
        trend_marker,
    )
    await database.record_fix_event(pool, kind="cookie_jar", detail=fix_detail)
    recorded = await database.latest_fix_event(pool, kind="cookie_jar")
    ok &= verdict(
        "a fix is recorded with its detail",
        recorded is not None and recorded[2] == fix_detail,
        f"count={await database.count_fix_events(pool)}",
    )
    await pool.execute(
        "UPDATE fix_events SET created_at = now() - make_interval(days => 2) WHERE detail = $1",
        fix_detail,
    )
    latest = await database.latest_fix_event(pool, kind="cookie_jar")
    trend = await telemetry.build_trend(pool, days=telemetry.TREND_DAYS)
    ok &= verdict(
        "the trend reads the same last fix",
        latest is not None
        and trend.last_fix is not None
        and (trend.last_fix.at, trend.last_fix.detail) == (latest[0], latest[2]),
        f"fix {latest[2] if latest else None}",
    )
    if trend.last_fix is not None and trend.last_fix.detail == fix_detail:
        login_effect = next(e for e in trend.effects if e.cause == "login")
        # *What* the rates say depends on the production rows sharing the window,
        # so this checks the plumbing: both windows are measured, the backdated
        # failures land in "before", and the rate is the count over the days.
        ok &= verdict(
            "a fix older than the guard is weighed over the two windows",
            bool(trend.effects)
            and not trend.too_soon
            and login_effect.before >= 4
            and login_effect.after >= 1
            and 1.5 <= login_effect.after_days <= 2.5
            and abs(login_effect.before_rate - login_effect.before / login_effect.before_days) < 1e-9,
            f"login {login_effect.before} → {login_effect.after} "
            f"(روزی {login_effect.before_rate:.1f} → {login_effect.after_rate:.1f})",
        )
    else:
        print("  ⚠ another fix is newer than this run's — skipping the effect checks")
    rendered_trend = telemetry.render_trend(trend)
    ok &= verdict(
        "the trend marks the fix and names the next step",
        "🔧" in rendered_trend and "قدم بعدی:" in rendered_trend,
        rendered_trend.splitlines()[0],
    )
    await pool.execute(
        """
        INSERT INTO fix_events (created_at, kind, detail)
        VALUES (now() - make_interval(days => 200), 'cookie_jar', 'ancient')
        """
    )
    pruned_fixes = await database.prune_fix_events(pool, keep_days=90)
    ok &= verdict("fixes older than the window are pruned", pruned_fixes >= 1, f"removed={pruned_fixes}")

    # --- cleanup ------------------------------------------------------------
    await pool.execute("DELETE FROM users WHERE telegram_id = ANY($1::bigint[])", [TEST_USER_A, TEST_USER_B])
    await pool.execute("DELETE FROM block_events WHERE url_host = $1", marker)
    await pool.execute("DELETE FROM block_events WHERE url_host = $1", trend_marker)
    await pool.execute("DELETE FROM fix_events WHERE detail = $1", fix_detail)
    if settings.admin_ids:  # the alarm check added its own rows, under its own host
        await pool.execute("DELETE FROM block_events WHERE url_host = $1", alert_marker)
    await pool.execute("DELETE FROM bot_state WHERE key = $1", "smoke_state")
    await pool.execute("DELETE FROM bot_state WHERE key = $1", database.SUPPORT_CONTACT_KEY)
    await r.aclose()
    await pool.close()

    if WARNINGS:
        print("\nWarnings (not failures):")
        for w in WARNINGS:
            print(f"  - {w}")
    print(f"\n== {'ALL PASS' if ok else 'FAILURES PRESENT'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
