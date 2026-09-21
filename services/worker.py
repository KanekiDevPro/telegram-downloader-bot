"""Background workers: pop tasks from the queue, download, upload, cache.

Workers run as asyncio tasks inside the same process but stay fully decoupled
from the Gateway: they only talk to the queue, DB, yt-dlp (in threads) and the
Telegram API. Heavy work never touches the bot's update loop.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import FSInputFile, InputMediaPhoto

from core import database
from core.config import get_settings
from core.utils import MediaFormat, escape_html, format_size, sanitize_filename, today_local
from services import cache as cache_service
from services import cookie_refresh, fallback, preflight, spotify, telemetry
from services.cobalt import CobaltError, CobaltService
from services.delivery import join_file_ids, send_album, send_cached_file
from services.extractor import (
    IMAGE_ONLY,
    DownloadResult,
    ExtractionError,
    ExtractorService,
    is_youtube_url,
    login_looking_block,
    youtube_login_hint,
)
from services.queue import DownloadTask, TaskQueue
from services.subscription import effective_daily_limit

logger = logging.getLogger(__name__)

PROGRESS_EDIT_INTERVAL_S = 4.0
MAX_ATTEMPTS = 3

#: One admin notice per window. A jar that cannot sign in fails *every* download,
#: and each of those already answers its own user — the admins need the fact once,
#: not once per link. Ten minutes is short enough to notice a second, unrelated
#: cause and long enough to stay silent through a burst of retries.
LOGIN_BLOCK_ALERT_INTERVAL_S = 600.0

#: Shown to the user whose link failed. No jargon (cookie/LOGIN_INFO) and no
#: blame: the fix is on the operator's side, and it is already in motion.
LOGIN_BLOCK_USER_MESSAGE = (
    "🔒 این لینک فقط با یک حساب واردشده (لاگین) قابل دانلود است و اتصال فعلی ربات "
    "اجازهٔ دانلود را ندارد — مشکل از سمت شما نیست.\n"
    "موضوع را به ادمین اطلاع دادیم؛ بعد از این‌که برطرف شد، همین لینک را دوباره بفرست."
)

# Error codes that retrying can never fix — fail fast and tell the user.
_PERMANENT_ERROR_CODES = {
    "UNSUPPORTED_URL",
    "PRIVATE_VIDEO",
    "AGE_RESTRICTED",
    "GEO_RESTRICTED",
    "LIVE_STREAM",
    "PLAYLIST_NOT_SUPPORTED",
    "FFMPEG_REQUIRED",
    # A post with no video in it has no video in it on the second attempt either.
    # The fallback (which serves the photos) runs before this list is consulted, so
    # this only saves the pointless retries when there is nobody to serve them.
    IMAGE_ONLY,
}

#: Extensions Telegram has a *photo* method for. The produced file decides, not the
#: request that asked for it: an image-only post answered as «video» still arrives as
#: a photo, and one cache row can be replayed as a video.
IMAGE_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp"})


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

async def run_worker(
    index: int,
    stop_event: asyncio.Event,
    bot: Bot,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    extractor: ExtractorService,
    cobalt: CobaltService | None = None,
) -> None:
    """One consumer loop: dequeue → process (with retries) → repeat.

    The loop survives queue outages (Redis restarts, network blips) with an
    exponential backoff instead of letting the worker task die silently.
    """
    logger.info("worker %s started", index)
    queue_errors = 0
    while not stop_event.is_set():
        try:
            task = await queue.dequeue()
        except Exception:
            if stop_event.is_set():
                # Shutdown cancels the blocked read, which redis-py reports as a
                # timeout — expected, not an incident worth a traceback.
                logger.info("worker %s: queue read interrupted by shutdown", index)
                break
            queue_errors += 1
            logger.exception("worker %s: could not read from the queue (attempt %s)", index, queue_errors)
            if await _sleep_until(stop_event, min(2**queue_errors, 30)):
                break
            continue
        queue_errors = 0
        if task is None:
            continue
        try:
            await _process_with_retry(task, bot, pool, queue, extractor, stop_event, cobalt)
        except Exception:
            logger.exception("worker %s: unexpected failure while processing %s", index, task.url)
    logger.info("worker %s stopped", index)


async def _sleep_until(stop_event: asyncio.Event, seconds: float) -> bool:
    """Sleep, waking early when shutdown is requested. True when it was."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def _requeue_for_shutdown(queue: TaskQueue, task: DownloadTask) -> None:
    """Hand an interrupted task back to the queue so a restart doesn't lose it."""
    try:
        await queue.requeue(task)
        logger.info("requeued interrupted task %s", task.url)
    except Exception:
        logger.exception("could not requeue interrupted task %s", task.url)


async def _process_with_retry(
    task: DownloadTask,
    bot: Bot,
    pool: asyncpg.Pool,
    queue: TaskQueue,
    extractor: ExtractorService,
    stop_event: asyncio.Event,
    cobalt: CobaltService | None = None,
) -> None:
    last_error: Optional[str] = None
    last_failure: ExtractionError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if stop_event.is_set():
            await _requeue_for_shutdown(queue, task)
            return
        try:
            await process_download_task(task, bot, pool, extractor, cobalt)
            if is_youtube_url(task.url):
                # An anonymous YouTube download just worked, so "YouTube refuses
                # anonymous requests here" is no longer true — stop acting on it.
                preflight.clear_anonymous_refusal()
            return
        except ExtractionError as exc:
            last_error = exc.message
            last_failure = exc
            logger.warning("attempt %s/%s failed for %s (%s)", attempt, MAX_ATTEMPTS, task.url, exc.code)
            if exc.code in _PERMANENT_ERROR_CODES or fallback.fallback_was_attempted(exc):
                # The second reason: both engines have already had this link. One
                # more round of "blocked, then blocked elsewhere" costs the user
                # another wait and cannot change the answer.
                break
        except Exception as exc:
            last_error = f"خطای داخلی: {exc}"
            last_failure = None
            logger.exception("attempt %s/%s crashed for %s", attempt, MAX_ATTEMPTS, task.url)
        if await _sleep_until(stop_event, min(2**attempt, 8)):
            await _requeue_for_shutdown(queue, task)
            return
    if last_failure is not None:
        # Recorded before the messages: the digest is the only place a *pattern*
        # of failures is visible, and it must survive a failed notification.
        await telemetry.record_block(pool, task, last_failure, extractor.cookie_file)
        # The weekly report is the trend; this is the alarm. A jar that stopped
        # signing in fails every link, so a run of three should not wait a week.
        await telemetry.maybe_send_early_alert(pool, bot, get_settings().admin_ids)
    if last_failure is not None and login_looking_block(
        last_failure, task.url, extractor.cookie_file
    ):
        # "Blocked" usually reaches the user as a shrug and the admins as nothing
        # at all. Both are wrong here: the cause is known and fixable.
        await _notify_login_block(bot, task, extractor.cookie_file)
        # ...and the fix may be mechanical: read the profile, replace the jar,
        # prove it with a probe, report. Off unless COOKIE_AUTO_EXPORT is set.
        settings = get_settings()
        # ``pool`` is how the replacement is recorded, and /trend is what reads
        # that: "did the fix reduce the failures?" needs a row at the fix.
        await cookie_refresh.auto_refresh_jar(
            settings, extractor, bot, settings.admin_ids, pool=pool
        )
        return
    await _notify_failure(bot, task, last_error or "خطای نامشخص")


async def _notify_failure(bot: Bot, task: DownloadTask, message: str) -> None:
    try:
        await bot.send_message(task.chat_id, f"❌ دانلود انجام نشد:\n{message}")
    except Exception:
        logger.exception("could not notify failure to chat %s", task.chat_id)


#: When the admins were last told about a login-shaped block (see the interval).
_last_login_block_alert_at: float = float("-inf")


def _login_block_alert_due(now: float | None = None) -> bool:
    """Whether the admins should hear about a login-shaped block right now."""
    global _last_login_block_alert_at
    moment = time.monotonic() if now is None else now
    due = moment - _last_login_block_alert_at >= LOGIN_BLOCK_ALERT_INTERVAL_S
    if due:
        _last_login_block_alert_at = moment
    return due


async def _notify_login_block(
    bot: Bot, task: DownloadTask, cookie_file: Path | None
) -> None:
    """Answer the user with the actual cause, and the admins with the fix."""
    # Evidence for the next link: YouTube refused an anonymous request here just
    # now, which is what turns the preflight's suspicion into a refusal.
    preflight.note_anonymous_refusal()
    try:
        await bot.send_message(task.chat_id, LOGIN_BLOCK_USER_MESSAGE)
    except Exception:
        logger.exception("could not notify chat %s about a login-looking block", task.chat_id)

    if not _login_block_alert_due():
        logger.info(
            "login-looking block for %s — admins were told about this within the last "
            "%.0fs, so the user's message stands alone.",
            task.url,
            LOGIN_BLOCK_ALERT_INTERVAL_S,
        )
        return

    if hint := youtube_login_hint(cookie_file):
        cause = escape_html(hint)
    else:
        cause = (
            "کوکی قابل‌استفاده‌ای برای یوتیوب نیست (COOKIE_FILE خالی/غیرقابل خواندن) — "
            "هر درخواست ناشناس می‌رود."
        )
    text = (
        "🍪 <b>دانلودها به خاطر لاگین نبودن ربات رد می‌شوند</b>\n\n"
        f"آخرین لینک ناموفق: <code>{escape_html(task.url)}</code>\n"
        f"علت: {cause}\n\n"
        "👉 کوکی را تازه کنید و بفرستید؛ دانلود بعدی خودش برمی‌دارد (بدون ری‌استارت). "
        "گزارش کامل: /doctor"
    )
    settings = get_settings()
    notified = 0
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
            notified += 1
        except Exception:
            logger.exception("could not tell admin %s about the login-looking block", admin_id)
    if notified == 0:
        logger.warning(
            "login-shaped block on %s and no admin could be told — check ADMIN_IDS.", task.url
        )
    logger.info("login-shaped block on %s: told %s admin(s)", task.url, notified)


# ---------------------------------------------------------------------------
# Task processing
# ---------------------------------------------------------------------------

async def process_download_task(
    task: DownloadTask,
    bot: Bot,
    pool: asyncpg.Pool,
    extractor: ExtractorService,
    cobalt: CobaltService | None = None,
) -> None:
    settings = get_settings()
    status = await bot.send_message(task.chat_id, "🔄 در حال پردازش لینک…")

    # 1) Cache re-check (worker-side, guards concurrent duplicate requests).
    cached = await cache_service.get_cached(pool, task.url, task.media_format)
    if cached is not None:
        await _edit(status, "⚡️ لینک قبلاً دانلود شده؛ در حال ارسال از کش…")
        if await send_cached_file(bot, task.chat_id, cached):
            await _edit(status, "✅ ارسال شد (از حافظهٔ کش)")
            return
        await cache_service.forget(pool, task.url, task.media_format)

    # 1b) Neither engine can fetch a Spotify link (yt-dlp refuses the site by
    #     policy, Cobalt has no Spotify service), so the *link* is rewritten to the
    #     same song on YouTube first — see services/spotify.py. From here on this is
    #     the ordinary path: extraction, fallback, quota, cache and caption all work
    #     on the mapped video, while the cache key stays the link the user sent.
    target_url = task.url
    if spotify.is_spotify_url(task.url):
        await _edit(status, "🎵 لینک اسپاتیفای — نسخهٔ یوتیوبِ همین آهنگ پیدا می‌شود…")
        target = await spotify.youtube_target(task.url, extractor)
        target_url = target.url
        await _edit(
            status,
            f"🎵 <b>{escape_html(target.track.credit)}</b> — از نسخهٔ یوتیوب دانلود می‌شود…",
        )

    # 2) Metadata extraction (threaded, non-blocking). A site that refuses *this
    #    request* is the one failure a different address fixes, so it goes to the
    #    fallback engine instead of the user.
    try:
        media_info = await extractor.extract(target_url)
    except ExtractionError as exc:
        if not fallback.should_use_fallback(exc, cobalt):
            await _note_fallback_skip(pool, exc, cobalt)
            raise
        await _deliver_via_fallback(
            task, bot, pool, extractor, cobalt, status, exc, target_url=target_url
        )
        return
    if media_info.is_live:
        await _edit(status, "⛔️ پخش زنده قابل دانلود نیست.")
        return
    if media_info.filesize_approx and media_info.filesize_approx > settings.upload_limit_bytes:
        await _edit(
            status,
            f"⛔️ حجم فایل ({format_size(media_info.filesize_approx)}) از سقف "
            f"{settings.upload_limit_mb} مگابایت بیشتر است.",
        )
        return

    # 3) Atomic quota claim (free users' quota is enforced here, not in the gateway).
    if not await _claim_quota(pool, task, status):
        return

    # 4) Download with live progress (threaded + throttled edits).
    await _edit(status, f"⬇️ در حال دانلود: <b>{escape_html(media_info.title[:120])}</b>")
    progress = _ProgressEditor(status)
    try:
        result = await extractor.download(
            target_url, task.media_format, progress_hook=progress.hook
        )
    except ExtractionError as exc:
        if not fallback.should_use_fallback(exc, cobalt):
            await _note_fallback_skip(pool, exc, cobalt)
            raise
        await _deliver_via_fallback(
            task,
            bot,
            pool,
            extractor,
            cobalt,
            status,
            exc,
            quota_claimed=True,
            target_url=target_url,
        )
        return

    await _finish_upload(task, bot, pool, status, result)
    if target_url != task.url:
        # A *mapped* link just went through YouTube anonymously (search and all), so
        # "YouTube refuses anonymous requests here" is no longer true.
        preflight.clear_anonymous_refusal()


async def _claim_quota(pool: asyncpg.Pool, task: DownloadTask, status: Any) -> bool:
    """Reserve today's slot for this user and say so; ``False`` = nothing to run."""
    user = await database.get_user(pool, task.telegram_id)
    if user is None:
        await _edit(status, "⛔️ حساب کاربر پیدا نشد؛ دوباره /start بزن.")
        return False
    limit = effective_daily_limit(user)
    if not await database.can_claim_download(pool, task.telegram_id, limit, today_local()):
        await _edit(status, f"⛔️ سهمیهٔ دانلود امروز ({limit}) تمام شده است.")
        return False
    return True


async def _finish_upload(
    task: DownloadTask,
    bot: Bot,
    pool: asyncpg.Pool,
    status: Any,
    result: DownloadResult,
) -> None:
    """Ceiling check, upload, cache the file_id — and always drop the job dir.

    The *result* owns the metadata here, not the earlier extraction: the fallback
    engine has no metadata of its own, so one shape has to serve both engines.
    """
    settings = get_settings()
    try:
        files = (result.file_path, *result.extra_paths)
        actual_size = sum(path.stat().st_size for path in files)
        if actual_size > settings.upload_limit_bytes:
            await _edit(status, "⛔️ حجم فایل نهایی از سقف مجاز بیشتر است.")
            return

        # Upload to Telegram and remember the file_id (and how to send it again).
        await _edit(status, "⬆️ در حال ارسال به تلگرام…")
        delivered = await _upload(bot, task.chat_id, result)
        if delivered.cacheable:
            await cache_service.memorize(
                pool,
                url=task.url,
                platform=result.info.platform,
                telegram_file_id=delivered.file_id,
                quality=task.media_format,
                kind=delivered.kind,
            )
        await _edit(status, "✅ دانلود و ارسال شد.")
    finally:
        shutil.rmtree(result.file_path.parent, ignore_errors=True)  # per-job dir


async def _note_fallback_skip(
    pool: asyncpg.Pool, error: ExtractionError, cobalt: CobaltService | None
) -> None:
    """Record that a refused link needed the net and did not get it.

    Only a *refusal* is noted — a block, or yt-dlp's DRM verdict
    (:func:`services.fallback.skip_reason` returns nothing for anything else): a
    private video was never going to be handed over, and writing "the net was
    skipped" there would send an admin chasing a health problem that does not exist.
    """
    reason = fallback.skip_reason(error, cobalt)
    if reason:
        await fallback.remember_use(pool, fallback.USE_SKIPPED, reason)


async def _deliver_via_fallback(
    task: DownloadTask,
    bot: Bot,
    pool: asyncpg.Pool,
    extractor: ExtractorService,
    cobalt: CobaltService | None,
    status: Any,
    error: ExtractionError,
    *,
    quota_claimed: bool = False,
    target_url: Optional[str] = None,
) -> None:
    """Try the fallback engine on a blocked link, and finish the job if it works.

    ``target_url`` is the link the work actually happens on: a rewritten Spotify
    link (see ``services.spotify``) is a YouTube video by the time we get here, and
    the fallback has to be handed *that* — asking Cobalt for a Spotify URL would end
    in a service it does not have.

    The user is never told about engines: the status message says the primary route
    was refused and that an alternative is being used, and a success is a success.
    The failure is still recorded — the extraction failure is real, and the weekly
    digest is where an operator sees that the primary engine is degraded even
    though nobody complained.

    When the fallback fails too, the *original* error is raised again: that one is
    about the user's link (and drives their message, the admin notice and the jar
    refresh). The retry wrapper is told both engines were tried, so it does not
    spend three attempts re-running the same pair.
    """
    # What the fallback is *asked for*: the mapped stand-in when there is one (a
    # rewritten Spotify link is a YouTube video by now, and Cobalt serves those).
    source_url = target_url or task.url
    logger.warning(
        "yt-dlp could not serve %s (%s) — engaging the Cobalt fallback",
        source_url,
        error.code,
    )
    if cobalt is None or not cobalt.enabled:  # guarded by should_use_fallback
        raise error
    if not quota_claimed and not await _claim_quota(pool, task, status):
        return

    await _edit(status, "🛠 مسیر اصلی به این لینک دسترسی نداشت؛ از مسیر جایگزین دانلود می‌شود…")
    settings = get_settings()
    progress = _ProgressEditor(status)
    try:
        result = await fallback.fetch(
            cobalt,
            source_url,
            task.media_format,
            download_dir=extractor.download_dir,
            max_bytes=settings.upload_limit_bytes,
            progress_hook=progress.hook,
        )
    except CobaltError as exc:
        logger.warning(
            "Cobalt fallback failed for %s too — %s", source_url, fallback.describe(exc)
        )
        await fallback.remember_use(
            pool, fallback.USE_FAILED, fallback.describe(exc)
        )
        raise fallback.mark_fallback_attempted(error) from exc

    # Recorded *after* the fallback saved it and before the upload: the file is
    # the user's either way, and this row is what keeps the degradation visible.
    await telemetry.record_block(pool, task, error, extractor.cookie_file)
    await fallback.remember_use(pool, fallback.USE_USED)
    await _finish_upload(task, bot, pool, status, result)


def _file_id(media: Any) -> str:
    """Extract the file_id from a sent media object.

    Telegram practically always returns it; a missing one means the upload is
    unusable, so treat it as a failure and let the retry wrapper report it.
    """
    file_id = getattr(media, "file_id", None)
    if not file_id:
        raise RuntimeError("Telegram returned no file_id for the uploaded media")
    return str(file_id)


def _upload_caption(result: DownloadResult) -> str:
    """Caption for the uploaded media (title, size and duration when known)."""
    files = (result.file_path, *result.extra_paths)
    parts = [
        f"<b>{escape_html(result.info.title[:200])}</b>",
        f"🌐 {escape_html(result.info.platform)}",
        f"📦 {format_size(sum(path.stat().st_size for path in files))}",
    ]
    duration = _fmt_duration(result.info.duration)
    if duration:
        parts.append(f"⏱ {duration}")
    return "\n".join(parts)


def _input_file(path: Path) -> FSInputFile:
    """A fresh FSInputFile per request — a consumed one can't be re-sent."""
    return FSInputFile(path, filename=sanitize_filename(path.name))


@dataclass(frozen=True)
class Delivered:
    """What reached the chat, and how a cached replay should send it again.

    An empty ``file_id`` means "not worth remembering": a *mixed* post (photos and
    a video in one) arrives as several messages of different kinds, and one cache
    row cannot describe that — the next request downloads it again instead of
    replaying half of it, which is the honest failure mode.
    """

    file_id: str = ""
    kind: str = ""

    @property
    def cacheable(self) -> bool:
        return bool(self.file_id and self.kind)


def _delivery_kind(path: Path, media_format: str) -> str:
    """Which Telegram method this produced file needs.

    The *file* decides, not the request: a user who asked for «video» on an
    image-only post gets photos, and the extension is what says so. Everything else
    follows the format that was asked for, exactly as it always did.
    """
    if path.suffix.lower().lstrip(".") in IMAGE_EXTENSIONS:
        return "photo"
    return "audio" if media_format == "audio" else "video"


def _photo_id(message: Any) -> str:
    """The largest size of a sent photo — the one worth caching."""
    sizes = getattr(message, "photo", None) or []
    if not sizes:
        raise RuntimeError("Telegram returned no photo for the uploaded image")
    return str(sizes[-1].file_id)


async def _send_document(bot: Bot, chat_id: int, path: Path, caption: str) -> str:
    """The last resort that is never wrong: the bytes, as a file."""
    sent = await bot.send_document(chat_id, _input_file(path), caption=caption)
    return _file_id(sent.document)


async def _send_photos(bot: Bot, chat_id: int, images: list[Path], caption: str) -> Delivered:
    """One photo, or an album of them — plus the ids a replay needs.

    Telegram caps a *photo* well below what a camera or an instagram export produces,
    and refuses a group it cannot render. Neither is a reason to fail a link that is
    a picture: it goes as a document instead, which is still the content.
    """
    if len(images) == 1:
        try:
            single = await bot.send_photo(chat_id, _input_file(images[0]), caption=caption)
            return Delivered(file_id=_photo_id(single), kind="photo")
        except TelegramBadRequest:
            logger.info("Telegram refused a photo as a photo — sending it as a document")
            file_id = await _send_document(bot, chat_id, images[0], caption)
            return Delivered(file_id=file_id, kind="file")
    try:
        group = await send_album(
            bot,
            chat_id,
            [InputMediaPhoto(media=_input_file(path)) for path in images],
            caption,
        )
    except TelegramBadRequest:
        logger.info("Telegram refused a media group — sending each picture on its own")
        for path in images:
            await _send_document(bot, chat_id, path, caption)
        return Delivered()  # several messages of one kind are not one cache row
    ids = [_photo_id(message) for message in group]
    if len(ids) != len(images):  # pragma: no cover — Telegram answers one per photo
        logger.warning("sent %s photos but got %s ids back", len(images), len(ids))
    return Delivered(file_id=join_file_ids(ids), kind="photo_group")


async def _send_file(
    bot: Bot, chat_id: int, path: Path, media_format: MediaFormat, caption: str
) -> str:
    """Upload one file the way its format deserves; returns its file_id."""
    if media_format == "audio":
        sent = await bot.send_audio(chat_id, _input_file(path), caption=caption)
        return _file_id(sent.audio)
    try:
        sent = await bot.send_video(
            chat_id, _input_file(path), caption=caption, supports_streaming=True
        )
        return _file_id(sent.video)
    except TelegramBadRequest:
        return await _send_document(bot, chat_id, path, caption)


async def _upload(bot: Bot, chat_id: int, result: DownloadResult) -> Delivered:
    """Send what the download produced: one file, one photo, or a whole album.

    Telegram has a method per shape and the shape is the file's, so an image post
    served by the fallback is sent as a photo even though «video» was asked for —
    and an album of them as a media group, in the post's own order.
    """
    files = (result.file_path, *result.extra_paths)
    caption = _upload_caption(result)
    images = [path for path in files if _delivery_kind(path, result.media_format) == "photo"]
    others = [path for path in files if path not in images]
    if images and not others:
        return await _send_photos(bot, chat_id, images, caption)
    if images:
        # A mixed post: the album first, then each video on its own. Deliberately
        # not cached (see ``Delivered``).
        delivered = await _send_photos(bot, chat_id, images, caption)
        for path in others:
            await _send_file(bot, chat_id, path, result.media_format, caption)
        return Delivered(kind=delivered.kind)
    return Delivered(
        file_id=await _send_file(bot, chat_id, files[0], result.media_format, caption),
        kind=_delivery_kind(files[0], result.media_format),
    )


async def _edit(status: Any, text: str) -> None:
    """Edit the status message, tolerating deleted/blocked chats."""
    try:
        await status.edit_text(text, disable_web_page_preview=True)
    except (TelegramBadRequest, TelegramRetryAfter):
        pass


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


class _ProgressEditor:
    """Throttled, thread-safe progress updates for yt-dlp hooks.

    yt-dlp calls the hook from a worker thread; edits are scheduled back onto
    the event loop via ``call_soon_threadsafe``.
    """

    def __init__(self, status: Any) -> None:
        self._status = status
        self._loop = asyncio.get_running_loop()
        self._last_edit = 0.0

    def hook(self, data: dict[str, Any]) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        pct = (done / total * 100) if total else 0.0
        now = time.monotonic()
        if pct < 100 and now - self._last_edit < PROGRESS_EDIT_INTERVAL_S:
            return  # throttle Telegram API calls
        self._last_edit = now
        text = f"⬇️ دانلود: {pct:.0f}% ({format_size(done)} / {format_size(total)})"
        self._loop.call_soon_threadsafe(asyncio.create_task, self._edit(text))

    async def _edit(self, text: str) -> None:
        await _edit(self._status, text)


# ---------------------------------------------------------------------------
# Maintenance loop
# ---------------------------------------------------------------------------

async def run_maintenance(
    stop_event: asyncio.Event,
    pool: asyncpg.Pool,
    bot: Bot | None = None,
    admin_ids: Iterable[int] = (),
) -> None:
    """Hourly sweep: expire premium, purge stale jobs, prune telemetry, report weekly."""
    logger.info("maintenance loop started")
    while not stop_event.is_set():
        try:
            expired = await database.expire_premiums(pool)
            if expired:
                logger.info("expired %s premium account(s)", expired)
            _cleanup_stale_jobs()
            await database.prune_block_events(pool, telemetry.KEEP_DAYS)
            await database.prune_helper_events(pool, telemetry.KEEP_DAYS)
            if bot is not None:
                # Weekly, and silent when nothing failed *and* no helper was down
                # (see maybe_send_digest).
                await telemetry.maybe_send_digest(pool, bot, admin_ids)
        except Exception:
            logger.exception("maintenance cycle failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
    logger.info("maintenance stopped")


def _cleanup_stale_jobs() -> None:
    settings = get_settings()
    cutoff = time.time() - 86400  # keep dirs younger than 1 day
    for directory in settings.download_dir.glob("job-*"):
        try:
            if directory.stat().st_mtime < cutoff:
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            pass
