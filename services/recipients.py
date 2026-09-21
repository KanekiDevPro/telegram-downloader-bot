"""Who gets an operator message, and in what language.

An admin notice has two audiences in one list: the person who configured the bot
(often reading English) and the operator who runs it day to day (often Persian).
Sending one text in one language means one of them reads a language they did not
choose — so the *recipients* are resolved with their stored language first, and the
message is rendered per recipient afterwards.

The resolution is one query for the whole list and never fatal: a notice that cannot
look up a language is still a notice, and dropping it (or raising) because a lookup
failed would turn a small problem into a silent one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import asyncpg

from core import database
from core.i18n import DEFAULT_LANG, normalize_lang

logger = logging.getLogger(__name__)

__all__ = ["targets"]


async def targets(
    pool: asyncpg.Pool, telegram_ids: Iterable[int], *, fallback: str = DEFAULT_LANG
) -> list[tuple[int, str]]:
    """``[(telegram_id, language)]`` for each recipient, in the order given.

    ``fallback`` is the language to use for an admin who has never spoken to the bot
    (there is no stored preference then), and it comes from the caller because only
    the caller knows whose notification this is.
    """
    ids = list(telegram_ids)
    if not ids:
        return []
    try:
        stored = await database.languages_for(pool, ids)
    except Exception:
        # A notice is more important than the language it arrives in.
        logger.exception("could not read recipient languages — falling back to %s", fallback)
        stored = {}
    return [(tg_id, normalize_lang(stored.get(tg_id), fallback)) for tg_id in ids]
