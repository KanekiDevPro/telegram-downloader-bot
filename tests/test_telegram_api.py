"""Session-wiring tests for the optional local Telegram Bot API server.

Constructing an ``AiohttpSession`` opens no sockets, so all of this runs offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram import Bot
from aiogram.client.telegram import SimpleFilesPathWrapper
from aiogram.types import User

import main
from core.config import Settings
from core.telegram_api import build_session, session_target


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_no_session_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # None means "let aiogram use the official cloud API".
    assert build_session(_settings(monkeypatch, TELEGRAM_API_BASE_URL="")) is None


def test_session_points_at_the_local_server(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, TELEGRAM_API_BASE_URL="http://telegram-api:8081")
    session = build_session(settings)

    assert session is not None
    assert session.api.base == "http://telegram-api:8081/bot{token}/{method}"
    assert session.api.file == "http://telegram-api:8081/file/bot{token}/{path}"
    assert session.api.api_url("123:abc", "getMe") == "http://telegram-api:8081/bot123:abc/getMe"


def test_trailing_slash_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_session(_settings(monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081/"))
    assert session is not None
    assert "//bot" not in session.api.base


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("0", False), ("", False)])
def test_local_mode_flag(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    settings = _settings(
        monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081", TELEGRAM_API_LOCAL=raw
    )
    assert settings.telegram_api_local is expected
    session = build_session(settings)
    assert session is not None
    assert session.api.is_local is expected


def test_files_path_wrapper_maps_server_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        TELEGRAM_API_BASE_URL="http://tg:8081",
        TELEGRAM_API_FILES_DIR="/srv/telegram-files",
    )
    session = build_session(settings)

    assert session is not None
    wrapper = session.api.wrap_local_file
    assert isinstance(wrapper, SimpleFilesPathWrapper)
    # The server-side prefix is swapped for the bot's own mount, and only the
    # relative part is carried over. Compare against the resolved setting so the
    # assertion holds on any OS.
    assert settings.telegram_api_files_dir is not None
    mapped = Path(wrapper.to_local("/var/lib/telegram-bot-api/bot123/video.mp4"))
    assert mapped == settings.telegram_api_files_dir / "bot123" / "video.mp4"


def test_no_wrapper_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    session = build_session(_settings(monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081"))
    assert session is not None
    assert session.api.wrap_local_file.to_local("/any/path") == "/any/path"


def test_uses_local_api_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, TELEGRAM_API_BASE_URL="").uses_local_api is False
    assert (
        _settings(
            monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081", TELEGRAM_API_ID="42", TELEGRAM_API_HASH="h"
        ).uses_local_api
        is True
    )


def test_local_api_configured_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = _settings(
        monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081", TELEGRAM_API_ID="", TELEGRAM_API_HASH=""
    )
    assert missing.local_api_configured is False

    complete = _settings(
        monkeypatch, TELEGRAM_API_ID="12345", TELEGRAM_API_HASH="deadbeef"
    )
    assert complete.local_api_configured is True


def test_session_target_label(monkeypatch: pytest.MonkeyPatch) -> None:
    assert session_target(_settings(monkeypatch, TELEGRAM_API_BASE_URL="")) == (
        "official cloud API (api.telegram.org)"
    )
    assert (
        session_target(_settings(monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081"))
        == "http://tg:8081"
    )
    settings = _settings(monkeypatch, TELEGRAM_API_BASE_URL="http://tg:8081")
    settings.use_cloud_api_fallback()
    assert session_target(settings) == "official cloud API (api.telegram.org)"


# ---------------------------------------------------------------------------
# connect_bot: an unreachable local server must not kill the bot
# ---------------------------------------------------------------------------

async def test_unreachable_local_server_falls_back_to_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch, BOT_TOKEN="123:abc", TELEGRAM_API_BASE_URL="http://telegram-api:8081"
    )

    async def fake_get_me(self: Bot) -> User:
        if self.session.api.base.startswith("http://telegram-api"):
            raise OSError("connection refused")
        return User(id=1, is_bot=True, first_name="t", username="t")

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    bot, me = await main.connect_bot(settings)

    assert settings.cloud_api_fallback is True
    assert settings.upload_limit_mb < settings.max_file_size_mb
    assert bot.session.api.base.startswith("https://api.telegram.org")
    assert me.username == "t"
    await bot.session.close()


async def test_reachable_local_server_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch, BOT_TOKEN="123:abc", TELEGRAM_API_BASE_URL="http://telegram-api:8081"
    )

    async def fake_get_me(self: Bot) -> User:
        return User(id=1, is_bot=True, first_name="t", username="t")

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    bot, _ = await main.connect_bot(settings)

    assert settings.cloud_api_fallback is False
    assert bot.session.api.base.startswith("http://telegram-api")
    await bot.session.close()


async def test_cloud_api_failure_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, BOT_TOKEN="123:abc", TELEGRAM_API_BASE_URL="")

    async def fake_get_me(self: Bot) -> User:
        raise OSError("no network")

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    with pytest.raises(OSError):
        await main.connect_bot(settings)
