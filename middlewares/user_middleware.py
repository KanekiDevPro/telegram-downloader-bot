"""Ensures a ``users`` row exists for every incoming update.

Attaches the fresh user record to ``data["user"]`` so handlers never worry
about registration — and the language that record carries to ``data["lang"]``, so a
handler (or a background task deriving from it) only has to ask for ``lang`` to
answer in the right one. ``lang`` is resolved here rather than in each handler
because the two questions "who is this" and "what language do they read" have the
same answer everywhere and would otherwise be re-asked a dozen times.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from core import database
from core.config import get_settings
from core.i18n import lang_of, normalize_lang


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        pool: asyncpg.Pool | None = data.get("pool")
        if pool is not None and isinstance(event, (Message, CallbackQuery)):
            from_user = event.from_user
            if from_user is not None:
                settings = get_settings()
                # A first contact starts in the client's own language when this bot
                # speaks it, otherwise in the deployment's default (DEFAULT_LANGUAGE).
                # Stored users keep what they have: the value only reaches an INSERT.
                record = await database.get_or_create_user(
                    pool,
                    from_user.id,
                    from_user.username,
                    normalize_lang(from_user.language_code, settings.default_language),
                )
                data["user"] = record
                data["lang"] = lang_of(record, settings.default_language)
        return await handler(event, data)
