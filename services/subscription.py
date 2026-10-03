"""Subscription / entitlement helpers (premium status, effective daily limits).

Admins are not a special case of premium, they are a third state: they never pay,
never expire, and are never stopped by a quota. That state is decided from
``ADMIN_IDS`` on every call rather than written into the database, for two reasons —
an operator who adds their own id to ``.env`` should not have to also remember a
subscription record, and an id removed from ``.env`` must lose the privileges of the
bot on the next check rather than at the end of a billing period.

The quota is a sentinel and not "infinity": the worker counts the download anyway
(one integer, and the profile shows the real number), and only the *ceiling* is out
of reach. Nothing in the download path needs to know about admins.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from core import database
from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: The daily ceiling an admin is measured against: far past any real use, so no
#: check has to be skipped and no counter has to be special-cased. The profile says
#: «unlimited ♾» instead of printing it.
UNLIMITED_DAILY_LIMIT = 10**7


def is_admin_id(telegram_id: int | str | None) -> bool:
    """Whether this Telegram id is in ``ADMIN_IDS`` (see ``Settings.is_admin``)."""
    return get_settings().is_admin(telegram_id)


def is_admin(user: asyncpg.Record) -> bool:
    """Whether this user record belongs to an admin."""
    try:
        return is_admin_id(user["telegram_id"])
    except (KeyError, IndexError, TypeError):  # a stub/record without the column
        return False


def is_premium_active(user: asyncpg.Record) -> bool:
    """Premium flag that also respects the expiry timestamp — admins always count.

    A payment check that an admin can fail is a payment check an operator has to
    work around, which is how "temporarily" becomes a hard-coded exception somewhere
    else.
    """
    if is_admin(user):
        return True
    if not user["is_premium"]:
        return False
    until = user["premium_until"]
    return until is None or until > datetime.now(timezone.utc)


def effective_daily_limit(user: asyncpg.Record) -> int:
    """Daily download quota for a user (premium gets the higher tier)."""
    if is_admin(user):
        return UNLIMITED_DAILY_LIMIT
    settings = get_settings()
    return settings.premium_daily_limit if is_premium_active(user) else settings.default_daily_limit


# ---------------------------------------------------------------------------
# Force-join: channels/groups a regular user must have joined before
# downloading. Disabled by default (empty FORCE_JOIN_TARGETS); when disabled
# nothing here is built and no check ever runs.
# ---------------------------------------------------------------------------

#: Redis namespace for remembered passes — positive results only, so a user
#: who just joined passes immediately. New keys (no migration): ``fj:ok:``
#: plus the target key plus the user id.
FORCE_JOIN_CACHE_PREFIX = "fj:ok:"

#: ``bot_state`` key prefix for the per-target admin-notice throttle, and its
#: quiet window (same 6h pattern as the storage/Redis notices in main.py).
FORCE_JOIN_NOTICE_KEY_PREFIX = "force_join:notice:"
FORCE_JOIN_NOTICE_COOLDOWN_S = 6 * 3600.0

#: One membership probe never waits longer than this, and a whole intake never
#: waits longer than the total — a slow Telegram never stalls a download.
FORCE_JOIN_CHECK_TIMEOUT_S = 5.0
FORCE_JOIN_TOTAL_TIMEOUT_S = 20.0

_USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{5,32}")
_CHAT_ID_RE = re.compile(r"-?[0-9]+")

#: What the admins hear, once per target per window, when a check cannot run
#: (bot removed, not an admin, Telegram trouble): fail-open, said out loud.
#: A code constant, not catalogue copy — same Persian-HTML shape as the other
#: operator notices in main.py.
_FORCE_JOIN_ADMIN_NOTICE = (
    "⚠️ <b>بررسی عضویت اجباری ناموفق بود</b>\n"
    "هدف: {target}\n"
    "علت: {reason}\n"
    "رفتار: fail-open — کاربر عبور کرد.\n"
    "قدم بعدی: دسترسی ربات به کانال/گروه را بررسی کنید."
)


@dataclass(frozen=True)
class ForceJoinTarget:
    """One channel/group, parsed once at startup into this immutable shape."""

    #: Cache/notice identity: the lowercased username or the chat id.
    key: str
    #: What ``get_chat_member`` receives: ``@username`` or the numeric id.
    chat: str | int
    #: Button and list label, from the operator's config.
    title: str
    #: Button URL: the public link, or the configured invite for privates.
    url: str


def _parse_force_join_entry(entry: str) -> ForceJoinTarget | None:
    """One comma-separated entry, or None when it names nothing checkable."""
    if entry.startswith("@"):
        if _USERNAME_RE.fullmatch(entry) is None:
            return None
        name = entry[1:]
        return ForceJoinTarget(
            key=name.lower(), chat=entry, title=entry, url=f"https://t.me/{name}"
        )
    parts = [part.strip() for part in entry.split("|")]
    if len(parts) != 3:
        return None
    chat_id, invite, title = parts
    if _CHAT_ID_RE.fullmatch(chat_id) is None:
        return None
    if not invite.startswith("https://") or not title:
        return None
    try:
        chat = int(chat_id)
    except ValueError:
        return None
    return ForceJoinTarget(key=chat_id, chat=chat, title=title, url=invite)


def parse_force_join_targets(raw: str) -> tuple[tuple[ForceJoinTarget, ...], tuple[str, ...]]:
    """Split ``FORCE_JOIN_TARGETS`` into (valid targets, invalid entries).

    Pure: no logging, no state — the caller decides how loudly to skip.
    """
    valid: list[ForceJoinTarget] = []
    invalid: list[str] = []
    for piece in (raw or "").split(","):
        entry = piece.strip()
        if not entry:
            continue
        target = _parse_force_join_entry(entry)
        if target is None:
            invalid.append(entry)
        else:
            valid.append(target)
    return (tuple(valid), tuple(invalid))


def _is_member_status(member: Any) -> bool:
    """The Bot API verdict: member/administrator/creator, and restricted only
    when Telegram still counts the user as a member. left/kicked are out."""
    status = getattr(member, "status", None)
    if status in ("member", "administrator", "creator"):
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def _notify_admins(bot: Bot, admin_ids: Iterable[int], text: str) -> None:
    """Best-effort operator notice — one unreachable admin is not an incident.

    Local (mirrors ``main.notify_admins``) so this module never imports the
    entrypoint: rare, throttled pages must never stall the intake that
    triggered them, so they skip the flood helper on purpose.
    """
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 — a notice must never break intake
            logger.warning("could not send a force-join notice to %s", admin_id)


class ForceJoinService:
    """The membership gate. State lives here, never at module level.

    The check is best-effort and fails open: Redis missing means Telegram is
    asked directly (never raising to the user), and Telegram trouble means
    the user goes through while the admins hear about it once per target.
    ``get_chat_member`` is a read call — deliberately outside the send/edit
    flood helper, which only wraps the join message itself.
    """

    def __init__(
        self,
        *,
        bot: Bot,
        redis: Any,
        pool: Any,
        admin_ids: Iterable[int],
        targets: tuple[ForceJoinTarget, ...],
        cache_ttl_s: int,
    ) -> None:
        self._bot = bot
        self._redis = redis
        self._pool = pool
        self._admin_ids = list(admin_ids)
        self._targets = targets
        self._cache_ttl_s = cache_ttl_s
        #: Last admin notice per target (process-level throttle when the pool
        #: is unreachable; the ``bot_state`` row covers restarts and replicas).
        self._last_notice: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        """Whether any target is configured — the only gate that matters."""
        return bool(self._targets)

    @property
    def targets(self) -> tuple[ForceJoinTarget, ...]:
        return self._targets

    def _cache_key(self, target: ForceJoinTarget, user_id: int) -> str:
        return f"{FORCE_JOIN_CACHE_PREFIX}{target.key}:{user_id}"

    async def _cached_pass(self, target: ForceJoinTarget, user_id: int) -> bool:
        redis = self._redis
        if redis is None:
            return False
        try:
            return await redis.get(self._cache_key(target, user_id)) is not None
        except Exception:  # noqa: BLE001 — a dead cache means ask Telegram
            logger.debug("force-join cache unreadable — asking Telegram", exc_info=True)
            return False

    async def _remember_pass(self, target: ForceJoinTarget, user_id: int) -> None:
        redis = self._redis
        if redis is None:
            return
        try:
            # int() on purpose: redis-py raises DataError on a float EX, and
            # a hand-rolled fake would never catch it (see guardfix).
            await redis.set(self._cache_key(target, user_id), "1", ex=int(self._cache_ttl_s))
        except Exception:  # noqa: BLE001 — best-effort cache, never fatal
            logger.debug("force-join cache unwritable — continuing", exc_info=True)

    async def _notice_due(self, target_key: str) -> bool:
        now = time.monotonic()
        last_local = self._last_notice.get(target_key)
        if last_local is not None and now - last_local < FORCE_JOIN_NOTICE_COOLDOWN_S:
            return False
        if self._pool is None:
            return True
        try:
            last = await database.get_state(
                self._pool, f"{FORCE_JOIN_NOTICE_KEY_PREFIX}{target_key}"
            )
        except Exception:  # noqa: BLE001 — the instance dict still guards us
            logger.debug("force-join throttle unreadable — notifying", exc_info=True)
            return True
        if last is None:
            return True
        try:
            return (
                datetime.now(timezone.utc) - datetime.fromisoformat(last)
            ).total_seconds() >= FORCE_JOIN_NOTICE_COOLDOWN_S
        except ValueError:
            logger.warning(
                "bot_state[force_join:notice:%s] is not a timestamp — treating it as due",
                target_key,
            )
            return True

    async def _stamp_notice(self, target_key: str) -> None:
        self._last_notice[target_key] = time.monotonic()
        if self._pool is None:
            return
        try:
            await database.set_state(
                self._pool,
                f"{FORCE_JOIN_NOTICE_KEY_PREFIX}{target_key}",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception:  # noqa: BLE001 — the stamp is a nicety, not the page
            logger.debug("could not record a force-join notice", exc_info=True)

    async def _degraded(self, target: ForceJoinTarget, reason: str) -> None:
        """One warning plus at most one throttled admin notice per target."""
        logger.warning("force-join check for %s failed (%s)", target.key, reason)
        if await self._notice_due(target.key):
            await _notify_admins(
                self._bot,
                self._admin_ids,
                _FORCE_JOIN_ADMIN_NOTICE.format(
                    target=html.escape(target.title), reason=html.escape(reason)
                ),
            )
            await self._stamp_notice(target.key)

    async def _check_one(self, target: ForceJoinTarget, user_id: int, *, fresh: bool) -> bool:
        """True when this target counts the user as joined (or cannot be
        checked — fail-open). Only passes are cached, never negatives."""
        if not fresh and await self._cached_pass(target, user_id):
            return True
        try:
            member = await asyncio.wait_for(
                self._bot.get_chat_member(target.chat, user_id),
                FORCE_JOIN_CHECK_TIMEOUT_S,
            )
        except TelegramRetryAfter as exc:
            # A flood refusal during a *read*: fail open without sleeping —
            # the intake path never waits out a flood.
            await self._degraded(target, f"flood control asked {exc.retry_after}s — failing open")
            return True
        except Exception as exc:  # noqa: BLE001 — any trouble fails open
            await self._degraded(target, f"{type(exc).__name__}: {str(exc)[:120]} — failing open")
            return True
        if _is_member_status(member):
            await self._remember_pass(target, user_id)
            return True
        return False

    async def _unjoined(self, user_id: int, *, fresh: bool) -> tuple[ForceJoinTarget, ...]:
        checks = [self._check_one(target, user_id, fresh=fresh) for target in self._targets]
        results = await asyncio.gather(*checks, return_exceptions=True)
        missing: list[ForceJoinTarget] = []
        for target, result in zip(self._targets, results):
            if result is True:
                continue
            if isinstance(result, BaseException):
                # Unreachable: _check_one absorbs everything — but a fail-open
                # gate must stay open even for its own bugs.
                logger.warning(
                    "force-join check for %s raised %r — failing open", target.key, result
                )
                continue
            missing.append(target)
        return tuple(missing)

    async def missing(self, user: asyncpg.Record, user_id: int) -> tuple[ForceJoinTarget, ...]:
        """Targets this user still has to join — () when clear or fail-open.

        Premium/VIP users and ADMIN_IDS bypass (``is_premium_active`` counts
        admins). The caller gates private chats; this only answers membership.
        Never raises to the user.
        """
        if not self._targets or is_premium_active(user):
            return ()
        try:
            return await asyncio.wait_for(
                self._unjoined(user_id, fresh=False), FORCE_JOIN_TOTAL_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — a stalled check is still fail-open
            logger.warning("force-join check stalled for user %s — failing open", user_id)
            return ()

    async def recheck(self, user_id: int) -> tuple[ForceJoinTarget, ...]:
        """A fresh Telegram check ignoring the cache — the verify button.

        Passes it confirms are still remembered, so the next link is fast.
        """
        try:
            return await asyncio.wait_for(
                self._unjoined(user_id, fresh=True), FORCE_JOIN_TOTAL_TIMEOUT_S
            )
        except Exception:  # noqa: BLE001 — a stalled recheck is still fail-open
            logger.warning("force-join recheck stalled for user %s — failing open", user_id)
            return ()

    async def validate_bot_access(self) -> tuple[str, ...]:
        """Whether the bot itself can see every target (startup validation).

        Returns the keys the bot cannot use. Problems are warned + paged like
        any other check failure; the bot being absent is fail-open, never fatal.
        Never raises.
        """
        if not self._targets:
            return ()
        try:
            me: int = self._bot.id
        except Exception:  # noqa: BLE001 — without an id there is no check
            logger.warning("force-join startup validation has no bot id — skipping")
            return ()
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *[self._check_one(target, me, fresh=True) for target in self._targets],
                    return_exceptions=True,
                ),
                FORCE_JOIN_TOTAL_TIMEOUT_S,
            )
        except Exception:  # noqa: BLE001 — validation must never block boot
            logger.warning("force-join startup validation timed out — continuing without it")
            return ()
        bad: list[str] = []
        for target, result in zip(self._targets, results):
            if result is True or isinstance(result, BaseException):
                continue
            bad.append(target.key)
            await self._degraded(target, "the bot is not a member of this chat — failing open")
        return tuple(bad)


def build_force_join(
    settings: Settings,
    *,
    bot: Bot,
    redis: Any,
    pool: Any,
    admin_ids: Iterable[int],
    warn_invalid: bool = True,
) -> ForceJoinService | None:
    """Parse ``FORCE_JOIN_TARGETS`` once into immutable targets, or None.

    None means disabled: the caller registers nothing and no check ever runs.
    Invalid entries are skipped with one startup warning, never a crash.
    """
    targets, invalid = parse_force_join_targets(settings.force_join_targets)
    if invalid and warn_invalid:
        logger.warning(
            "FORCE_JOIN_TARGETS skips %d invalid entr%s: %s",
            len(invalid),
            "y" if len(invalid) == 1 else "ies",
            "; ".join(invalid),
        )
    if not targets:
        return None
    return ForceJoinService(
        bot=bot,
        redis=redis,
        pool=pool,
        admin_ids=admin_ids,
        targets=targets,
        cache_ttl_s=settings.force_join_cache_ttl_s,
    )
