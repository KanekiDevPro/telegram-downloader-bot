"""Inline mode: ``@BotUsername <link>`` in any chat.

The inline path performs NO network I/O and NO DNS — only structural parsing
(``extract_url``, ``validate_url``, ``canonical_url``), pure platform
classification (``services.content``) and a cache lookup. A link that would
need redirect resolution, like every other uncertainty, becomes the deep-link
button; the full ``host_guard`` still runs in the normal intake after it.

Cached results go only to users who already exist in the ``users`` table (they
started the bot) and pass the read-only gates: quota left (checked, never
consumed — inline sends do NOT consume quota) and force-join (the ``fj:ok``
cache only, never a Telegram call, fail-open on Redis trouble). Unknown users
get only the plain start button; everyone else gets the deep link.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any

import asyncpg
from aiogram import Bot, Router
from aiogram.types import (
    InlineQuery,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultsButton,
)

from core import database
from core.config import get_settings
from core.i18n import Lang, lang_of, t
from core.utils import (
    canonical_url,
    extract_url,
    sha256_hex,
    today_local,
    validate_url,
)
from services import cache as cache_service
from services import content
from services.delivery import _kind_of, quality_label, replay_caption, split_file_ids
from services.subscription import effective_daily_limit

logger = logging.getLogger(__name__)
router = Router(name="inline")

#: At most this many cached requests come back for one query.
INLINE_MAX_RESULTS = 10
#: How long Telegram may reuse our answer (seconds): small, because quota and
#: membership change under it.
INLINE_CACHE_TIME = 10
#: Deep-link payload: ``dl_`` plus the existing 16-hex url digest — inside
#: Telegram's ``[A-Za-z0-9_-]{1,64}`` start_parameter grammar.
INLINE_TOKEN_PREFIX = "dl_"
INLINE_TOKEN_RE = re.compile(r"dl_[0-9a-f]{16}")
INLINE_TOKEN_RE_FULL = re.compile(r"\Adl_[0-9a-f]{16}\Z")
#: The Redis home of a deep-link token: the canonical URL, an int EX away
#: from expiring. Same URL, same key — no growth per keystroke.
INLINE_TOKEN_KEY_PREFIX = "dl:"
INLINE_TOKEN_TTL_S = 3600
#: Per-user token minting: INCR + int EXPIRE, about 30 a minute.
INLINE_TOKEN_RATE_KEY_PREFIX = "dl:rl:"
INLINE_TOKEN_RATE_LIMIT = 30
INLINE_TOKEN_RATE_WINDOW_S = 60

Result = (
    InlineQueryResultCachedVideo
    | InlineQueryResultCachedAudio
    | InlineQueryResultCachedPhoto
    | InlineQueryResultCachedDocument
)

#: What ``Bot.answer_inline_query`` takes: aiogram's own 28-member union.
#: Built as ``list[Any]`` — spelling the whole union here would pin this
#: module to aiogram's type catalogue for no runtime gain.
AnswerResults = list[Any]


def _today() -> Any:
    """Today's date boundary for the soft quota read (kept behind one name so
    tests read the same day the handler does)."""
    return today_local()


def _canonical(url: str) -> str:
    """The cache's own normal form — no redirect resolution, ever."""
    return canonical_url(url)


def _digest(url: str) -> str:
    """The existing 16-hex url digest: token payloads and result ids."""
    return sha256_hex(_canonical(url))[:16]


def _direct_url(url: str) -> str | None:
    """The URL when the inline path may serve it, else None.

    Structural checks only, then the pure classifier: a link the catalogue
    cannot claim (or one hiding behind a share wrapper) would need a network
    round trip to resolve, so it is deep-link material, not cache material.
    """
    if not validate_url(url):
        return None
    if content.unwrap_media_url(url) != url or not content.claims_platform(url):
        return None
    return url


def _quality_rank(quality: str) -> int:
    """Leading number of a quality label (``1080p`` → 1080), else -1."""
    match = re.match(r"(\d+)", (quality or "").strip())
    return int(match.group(1)) if match else -1


def _kind_rank(kind: str) -> int:
    return {"video": 0, "photo_group": 1, "photo": 2, "audio": 3}.get(kind, 4)


def _ordered(rows: Sequence[Any]) -> list[Any]:
    """Best quality first, as the intake menus order them; deterministic."""
    return sorted(
        rows,
        key=lambda row: (
            _kind_rank(_kind_of(row)),
            -_quality_rank(str(row["quality"] or "")),
            str(row["url_hash"] or ""),
        ),
    )


def _title_for(row: Any, kind: str, lang: str) -> str:
    quality = str(row["quality"] or "")
    if kind == "video":
        label = quality_label("video", row["quality"], lang)
        return label or quality or kind
    if kind == "audio":
        label = quality_label("audio", row["quality"], lang)
        return label or quality or kind
    return quality or kind


def _result_for(row: Any, index: int, digest: str, lang: str) -> Result | None:
    """One cache row → its cached result, or None when the row is unusable."""
    ids = split_file_ids(str(row["telegram_file_id"] or ""))
    if not ids:
        return None
    kind = _kind_of(row)
    result_id = f"{kind}:{digest}:{index}"
    caption = replay_caption(row, lang)
    title = _title_for(row, kind, lang)
    if kind == "video":
        return InlineQueryResultCachedVideo(
            id=result_id, video_file_id=ids[0], title=title, caption=caption
        )
    if kind == "audio":
        return InlineQueryResultCachedAudio(
            id=result_id, audio_file_id=ids[0], caption=caption
        )
    if kind in ("photo", "photo_group"):
        return InlineQueryResultCachedPhoto(
            id=result_id, photo_file_id=ids[0], caption=caption
        )
    return InlineQueryResultCachedDocument(
        id=result_id,
        document_file_id=ids[0],
        title=title,
        caption=caption,
    )


async def _quota_left(user: Any, pool: asyncpg.Pool) -> bool:
    """The intake's soft quota read: over the ceiling means button, not results."""
    try:
        usage = await database.get_daily_usage(pool, user["telegram_id"])
    except Exception:
        logger.debug("inline quota read failed — failing open", exc_info=True)
        return True
    used = (
        usage["daily_downloads"]
        if usage and usage["last_download_date"] == _today()
        else 0
    )
    return used < effective_daily_limit(user)


async def _membership_ok(force_join: Any, user_id: int) -> bool:
    """Force-join through the ``fj:ok`` cache only — never a Telegram call."""
    if force_join is None or not force_join.enabled:
        return True
    try:
        return bool(await force_join.cached_pass(user_id))
    except Exception:
        logger.debug("inline force-join cache unreadable — failing open", exc_info=True)
        return True


async def mint_token(
    redis: Any, url: str, user_id: int
) -> str | None:
    """The ``dl_<digest>`` payload for this URL, or None for plain start.

    Idempotent per URL (same key, same value); rate-limited per user
    (``INCR`` + int ``EXPIRE``); Redis trouble means plain start, never an
    error. Every ``ex=``/``expire`` is an int — redis-py raises ``DataError``
    on a float EX, and offline fakes would never catch it.
    """
    if redis is None:
        return None
    canonical = _canonical(url)
    digest = _digest(url)
    rate_key = f"{INLINE_TOKEN_RATE_KEY_PREFIX}{user_id}"
    try:
        count = await redis.incr(rate_key)
    except Exception:
        logger.debug("inline token rate counter unreadable — plain start", exc_info=True)
        return None
    try:
        if int(count) == 1:
            await redis.expire(rate_key, int(INLINE_TOKEN_RATE_WINDOW_S))
    except Exception:
        logger.debug("inline token rate window unset — continuing", exc_info=True)
    if int(count) > INLINE_TOKEN_RATE_LIMIT:
        logger.info("inline token rate limit hit for user %s", user_id)
        return None
    try:
        await redis.set(
            f"{INLINE_TOKEN_KEY_PREFIX}{digest}", canonical, ex=int(INLINE_TOKEN_TTL_S)
        )
    except Exception:
        logger.debug("inline token unwritable — plain start", exc_info=True)
        return None
    return f"{INLINE_TOKEN_PREFIX}{digest}"


async def lookup_token(redis: Any, payload: str) -> str | None:
    """The canonical URL behind a ``dl_<digest>`` payload, or None."""
    if redis is None or INLINE_TOKEN_RE_FULL.match(payload) is None:
        return None
    try:
        return await redis.get(f"{INLINE_TOKEN_KEY_PREFIX}{payload[3:]}")
    except Exception:
        logger.debug("inline token unreadable — treating it as expired", exc_info=True)
        return None


def _button(lang: Lang | str, payload: str | None) -> InlineQueryResultsButton:
    if payload:
        return InlineQueryResultsButton(
            text=t("inline.open_bot", lang), start_parameter=payload
        )
    return InlineQueryResultsButton(text=t("inline.open_bot_plain", lang))


async def on_inline_query(
    inline: InlineQuery,
    bot: Bot,
    pool: asyncpg.Pool | None = None,
    redis: Any = None,
    force_join: Any = None,
) -> None:
    """Answer ``@Bot <link>``: cached files for known users, else the way in.

    One answer per query, always. Cheap answers only: a flood refusal on the
    answer itself is absorbed and logged once, never slept out.
    """
    from aiogram.exceptions import TelegramRetryAfter

    text = (inline.query or "").strip()
    from_user = inline.from_user
    user_id = from_user.id if from_user is not None else 0

    async def _answer(
        results: AnswerResults, button: InlineQueryResultsButton | None
    ) -> None:
        try:
            await bot.answer_inline_query(
                inline.id,
                results=results,
                cache_time=int(INLINE_CACHE_TIME),
                is_personal=True,
                button=button,
            )
        except TelegramRetryAfter as exc:
            logger.warning(
                "inline answer flood-limited (%ss asked) — dropping it", exc.retry_after
            )
        except Exception:
            logger.warning("inline answer failed", exc_info=True)

    raw = extract_url(text)
    if not raw or pool is None or user_id == 0:
        await _answer([], None)
        return
    url = _direct_url(raw)
    try:
        user = await database.get_user(pool, user_id)
    except Exception:
        logger.debug("inline user lookup failed — start button only", exc_info=True)
        user = None
    if user is None:
        await _answer([], _button("en", None))
        return
    lang = lang_of(user, get_settings().default_language)
    payload = await mint_token(redis, raw, user_id)
    button = _button(lang, payload)
    if url is None:
        await _answer([], button)
        return
    if not await _quota_left(user, pool):
        await _answer([], button)
        return
    if not await _membership_ok(force_join, user_id):
        await _answer([], button)
        return
    try:
        rows = await cache_service.get_cached_rows(pool, url)
    except Exception:
        logger.debug("inline cache lookup failed — deep link only", exc_info=True)
        rows = []
    digest = _digest(url)
    results: AnswerResults = []
    for index, row in enumerate(_ordered(rows)):
        if len(results) >= INLINE_MAX_RESULTS:
            break
        result = _result_for(row, index, digest, lang)
        if result is not None:
            results.append(result)
    await _answer(results, button)
