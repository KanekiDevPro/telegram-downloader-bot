"""Ensures a ``users`` row exists for every incoming update.

Attaches the fresh user record to ``data["user"]`` so handlers never worry
about registration.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import asyncpg
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from core import database


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
                data["user"] = await database.get_or_create_user(
                    pool, from_user.id, from_user.username
                )
        return await handler(event, data)
