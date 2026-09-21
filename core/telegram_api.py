"""Aiogram session wiring, including the optional self-hosted Bot API server.

Why a local Bot API server matters here: the *cloud* Bot API only lets a bot
upload files up to 50 MB, so a `MAX_FILE_SIZE_MB=2000` config would look fine and
then fail on every real video. A local `telegram-bot-api` instance raises that
ceiling to 2000 MB (and 4 GB downloads in ``--local`` mode).

When ``TELEGRAM_API_BASE_URL`` is empty this module returns ``None`` and aiogram
talks to the official cloud API — no behaviour change for simple deployments.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer

from core.config import Settings

logger = logging.getLogger(__name__)

#: Port the aiogram/telegram-bot-api image listens on.
DEFAULT_LOCAL_API_PORT = 8081

#: Where telegram-bot-api stores the files it serves in local mode.
DEFAULT_SERVER_FILES_DIR = "/var/lib/telegram-bot-api"

__all__ = ["DEFAULT_LOCAL_API_PORT", "DEFAULT_SERVER_FILES_DIR", "build_session", "session_target"]


def build_session(settings: Settings) -> AiohttpSession | None:
    """Build the aiogram session for the configured API server.

    Returns ``None`` to use aiogram's default (official cloud API).
    """
    if not settings.uses_local_api:
        return None

    # Only pass wrap_local_file when we actually have a mapping: an explicit None
    # would override the dataclass default and break `Bot.download`.
    kwargs: dict[str, Any] = {"is_local": settings.telegram_api_local}
    wrapper = _files_path_wrapper(settings)
    if wrapper is not None:
        kwargs["wrap_local_file"] = wrapper
    api = TelegramAPIServer.from_base(settings.telegram_api_base_url, **kwargs)
    logger.info(
        "using local Bot API server at %s (local_mode=%s)",
        settings.telegram_api_base_url,
        settings.telegram_api_local,
    )
    if not settings.local_api_configured:
        logger.warning(
            "TELEGRAM_API_BASE_URL is set but TELEGRAM_API_ID/TELEGRAM_API_HASH are "
            "missing — the telegram-api container will not start without them."
        )
    return AiohttpSession(api=api)


def _files_path_wrapper(settings: Settings) -> SimpleFilesPathWrapper | None:
    """Map the server's file directory onto the bot's own mount, when configured.

    Only relevant for *downloads* in local mode; uploads always go over the
    Bot API socket.
    """
    if settings.telegram_api_files_dir is None:
        return None
    return SimpleFilesPathWrapper(
        server_path=Path(DEFAULT_SERVER_FILES_DIR),
        local_path=settings.telegram_api_files_dir,
    )


def session_target(settings: Settings) -> str:
    """Human-readable description of where API calls will *actually* go.

    Reports the cloud API once ``main.connect_bot`` has fallen back, so logs and
    boot checks never claim a local server is in use when it is not.
    """
    if settings.uses_local_api and not settings.cloud_api_fallback:
        return settings.telegram_api_base_url
    return "official cloud API (api.telegram.org)"
