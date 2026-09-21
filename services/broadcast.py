"""Global broadcast: one message, every user, without hammering Telegram.

Two things make this more than a ``for`` loop.

**Telegram's limits are real.** A bot may send roughly 30 messages a second, and
the penalty for finding out is a ``429`` that stops the whole run. Sends are
therefore paced (a small sleep between them, re-read from the API's own
``retry_after`` when it does push back), and a page of recipients at a time is
read from the database rather than the whole table — a deployment with a million
users should not need a million integers in memory to send one announcement.

**Failure is per recipient.** A user who blocked the bot (``403``) or deleted
their account is not a broken broadcast, so they are counted separately from real
errors, and no single failure stops the run. What comes back is a report an admin
can act on: how many were reached, how many are unreachable for good, how many
failed for a reason worth reading the log about.

The text is the admin's own message, sent as-is through the bot's default parse
mode (HTML), so the same formatting the operator sees in their draft is what the
users get.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncpg
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from core import database

logger = logging.getLogger(__name__)

#: Seconds between two sends. Telegram's practical ceiling is about 30/s for a
#: bot; staying at ~16/s leaves room for the rest of the bot's traffic (a download
#: finishing, a queue position update) instead of competing with it.
SEND_INTERVAL_S = 0.06

#: How often the caller's progress callback is told where the run is. Editing the
#: admin's message on every send would burn the rate limit the pacing is there for.
PROGRESS_EVERY = 25


@dataclass(frozen=True)
class BroadcastReport:
    """What a broadcast run did, in the four numbers that differ."""

    total: int = 0
    sent: int = 0
    #: ``403`` — the user blocked the bot (or deleted the account). Permanent, and
    #: not a failure: nothing about this run was wrong.
    blocked: int = 0
    #: Anything else: counted, logged, and kept out of ``sent``.
    failed: int = 0

    @property
    def ok(self) -> bool:
        return self.failed == 0


#: What a caller may pass to watch the run: ``(sent, total)``.
Progress = Callable[[int, int], Awaitable[None]]


async def _send_one(bot: Bot, telegram_id: int, text: str) -> int:
    """Send one message; returns the failure count (0 = delivered, 0 = blocked).

    ``retry_after`` is honoured once and then given up on, because the API's own
    number is authoritative and a second refusal means something else is going on
    (a genuinely oversized message, say) — which is a failure to report, not to
    wait out.
    """
    try:
        await bot.send_message(telegram_id, text)
        return 0
    except TelegramForbiddenError:
        return -1  # blocked: reported by the caller as such
    except TelegramRetryAfter as exc:
        logger.warning(
            "broadcast hit Telegram's rate limit — waiting %ss as instructed", exc.retry_after
        )
        await asyncio.sleep(float(exc.retry_after) + 0.5)
        try:
            await bot.send_message(telegram_id, text)
            return 0
        except TelegramForbiddenError:
            return -1
        except Exception:
            logger.exception("broadcast to %s failed after a rate-limit retry", telegram_id)
            return 1
    except TelegramBadRequest:
        logger.info("broadcast to %s refused (bad request)", telegram_id, exc_info=True)
        return 1
    except Exception:
        logger.exception("broadcast to %s failed", telegram_id)
        return 1


async def deliver(
    bot: Bot,
    pool: asyncpg.Pool,
    text: str,
    *,
    on_progress: Progress | None = None,
    page_size: int = database.BROADCAST_PAGE_SIZE,
    interval_s: float = SEND_INTERVAL_S,
) -> BroadcastReport:
    """Send ``text`` to every user, one page at a time, and report what happened.

    ``total`` is counted as the run goes (the pages are the source of truth, so the
    report cannot claim to have reached more people than there were rows).
    """
    sent = blocked = failed = total = 0
    after = 0
    while True:
        page = await database.user_id_page(pool, after, page_size)
        if not page:
            break
        for telegram_id in page:
            total += 1
            outcome = await _send_one(bot, telegram_id, text)
            if outcome == 0:
                sent += 1
            elif outcome < 0:
                blocked += 1
            else:
                failed += 1
            if on_progress is not None and total % PROGRESS_EVERY == 0:
                await _progress(on_progress, sent, total)
            if interval_s > 0:
                await asyncio.sleep(interval_s)
        after = page[-1]
    if on_progress is not None:
        await _progress(on_progress, sent, total)
    logger.info(
        "broadcast finished: %s sent, %s blocked the bot, %s failed (of %s)",
        sent,
        blocked,
        failed,
        total,
    )
    return BroadcastReport(total=total, sent=sent, blocked=blocked, failed=failed)


async def _progress(on_progress: Progress, sent: int, total: int) -> None:
    """Tell the caller where the run is — a failure here must not stop the run."""
    try:
        await on_progress(sent, total)
    except Exception:
        logger.debug("broadcast progress callback failed", exc_info=True)
