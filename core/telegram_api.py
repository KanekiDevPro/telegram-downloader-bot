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
from typing import Any, cast

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import SimpleFilesPathWrapper, TelegramAPIServer

from core.config import Settings, probe_url

logger = logging.getLogger(__name__)

#: Port the aiogram/telegram-bot-api image listens on.
DEFAULT_LOCAL_API_PORT = 8081

#: Where telegram-bot-api stores the files it serves in local mode.
DEFAULT_SERVER_FILES_DIR = "/var/lib/telegram-bot-api"

__all__ = [
    "DEFAULT_LOCAL_API_PORT",
    "DEFAULT_SERVER_FILES_DIR",
    "SettingsDrivenAPIServer",
    "build_session",
    "local_api_is_reachable",
    "local_file_uri",
    "session_target",
]


class SettingsDrivenAPIServer:
    """A Telegram API target whose aim follows ``settings.cloud_api_fallback``.

    Both directions, on every call — the flag is *state*, not a verdict. While
    it is set the URLs point at the official cloud API; the moment something
    clears it (``Settings.restore_local_api``), the very same session aims at
    the local server again: no restart, no rebuilt Bot, and the zero-copy file
    URI resumes with it (see ``services.worker._recover_local_api_if_healthy``).
    """

    def __init__(self, settings: Settings, local: TelegramAPIServer) -> None:
        self._settings = settings
        self._local = local
        self._cloud = TelegramAPIServer.from_base("https://api.telegram.org")

    def _target(self) -> TelegramAPIServer:
        if self._settings.cloud_api_fallback:
            return self._cloud
        return self._local

    @property
    def base(self) -> str:
        return self._target().base

    @property
    def file(self) -> str:
        return self._target().file

    @property
    def is_local(self) -> bool:
        return self._target().is_local

    @property
    def wrap_local_file(self) -> Any:
        return self._target().wrap_local_file

    def api_url(self, token: str, method: str) -> str:
        return self._target().api_url(token, method)

    def file_url(self, token: str, path: str | Path) -> str:
        return self._target().file_url(token, path)


async def local_api_is_reachable(settings: Settings) -> bool:
    """Does the local Bot API server answer at all? One cheap probe.

    The healthcheck's semantics, from outside the container: any HTTP response
    means a process is serving (the root path answers 404 by design). Used to
    un-latch ``cloud_api_fallback`` when the server is back — rate-limited by
    the caller (``services.worker``), never a per-send cost. The address is the
    one *this process* can reach (:func:`core.config.probe_url`).
    """
    if not settings.uses_local_api:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=2.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(probe_url(settings.telegram_api_base_url)) as response:
                return response.status > 0
    except (aiohttp.ClientError, TimeoutError, OSError):
        return False


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
    return AiohttpSession(
        api=cast("TelegramAPIServer", SettingsDrivenAPIServer(settings, api))
    )


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


def local_file_uri(path: Path, settings: Settings) -> str | None:
    """``file://`` URI for a zero-copy upload through a local Bot API server — or ``None``.

    A local server reads the file *itself* when the upload names its local path
    with the file URI scheme (core.telegram.org/bots/api: local servers accept
    uploads "using their local path and the file URI scheme"), so delivering a
    460 MB file becomes a metadata call instead of 460 MB through the socket.
    Three gates decide it:

    * the deployment is on a local server running in local mode and has not
      fallen back to the cloud — the cloud has never heard of a file URI;
    * ``TELEGRAM_API_SHARED_DIR`` names the directory the server mounts *at the
      same path* (the URI is the path itself, so a wrong guess would be a failed
      upload — an unconfigured shared dir keeps every upload streaming);
    * the file actually lives inside that directory.

    Anything else answers ``None``: stream the bytes, exactly as before.
    """
    shared = settings.telegram_api_shared_dir
    if not (
        settings.uses_local_api
        and settings.telegram_api_local
        and not settings.cloud_api_fallback
        and shared is not None
    ):
        return None
    resolved = path.resolve()
    if not resolved.is_relative_to(shared.resolve()):
        return None
    return resolved.as_uri()


def session_target(settings: Settings) -> str:
    """Human-readable description of where API calls will *actually* go.

    Reports the cloud API once ``main.connect_bot`` has fallen back, so logs and
    boot checks never claim a local server is in use when it is not.
    """
    if settings.uses_local_api and not settings.cloud_api_fallback:
        return settings.telegram_api_base_url
    return "official cloud API (api.telegram.org)"
