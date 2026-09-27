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
from core.telegram_api import build_session, local_file_uri, session_target


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


# ---------------------------------------------------------------------------
# Zero-copy upload: a file URI is offered only where the server can read it
# ---------------------------------------------------------------------------

def _shared_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str
) -> tuple[Settings, Path, Path]:
    shared = tmp_path / "shared"
    (shared / "job-1").mkdir(parents=True)
    inside = shared / "job-1" / "video.mp4"
    inside.write_bytes(b"x")
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    settings = _settings(
        monkeypatch,
        TELEGRAM_API_BASE_URL="http://telegram-api:8081",
        TELEGRAM_API_LOCAL="1",
        TELEGRAM_API_SHARED_DIR=str(shared),
        **extra,
    )
    return settings, inside, outside


def test_the_file_uri_is_offered_only_for_files_on_the_shared_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The local server reads the file *itself* — the same absolute path, seen
    through the volume both containers mount (identity on purpose: the URI must
    name the file where the server is, and a wrong guess is a failed upload).
    """
    settings, inside, outside = _shared_setup(monkeypatch, tmp_path)

    assert local_file_uri(inside, settings) == inside.resolve().as_uri()
    assert local_file_uri(outside, settings) is None, "outside the volume the server cannot see it"


def test_the_file_uri_never_outlives_a_fallback_to_the_cloud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cloud API has never heard of a file URI — after the local server is
    declared unreachable every upload must be bytes again."""
    settings, inside, _outside = _shared_setup(monkeypatch, tmp_path)
    settings.use_cloud_api_fallback()

    assert local_file_uri(inside, settings) is None


def test_no_uri_without_the_pieces_that_make_it_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    media = shared / "a.mp4"
    media.write_bytes(b"x")

    # No shared volume configured:
    plain = _settings(
        monkeypatch,
        TELEGRAM_API_BASE_URL="http://telegram-api:8081",
        TELEGRAM_API_LOCAL="1",
        TELEGRAM_API_SHARED_DIR="",
    )
    assert local_file_uri(media, plain) is None

    # The cloud API:
    cloud = _settings(
        monkeypatch,
        TELEGRAM_API_BASE_URL="",
        TELEGRAM_API_LOCAL="",
        TELEGRAM_API_SHARED_DIR=str(shared),
    )
    assert local_file_uri(media, cloud) is None

    # A local server without local mode (the 50 MB ceiling variant):
    not_local = _settings(
        monkeypatch,
        TELEGRAM_API_BASE_URL="http://telegram-api:8081",
        TELEGRAM_API_LOCAL="0",
        TELEGRAM_API_SHARED_DIR=str(shared),
    )
    assert local_file_uri(media, not_local) is None
