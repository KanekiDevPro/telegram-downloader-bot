"""Who the update is from, and what language they read.

Two questions with the same answer every time, so they are answered once, in the
middleware: the user's row (created on first contact) and the language every handler
gets handed. The rules pinned here are the ones that are easy to get subtly wrong:

* a first contact starts in the client's own language *when the bot speaks it* — a
  Persian client gets Persian without touching anything;
* a stored choice always wins — a user who picked English keeps it, whatever locale
  their client reports later;
* a locale the bot does not speak lands on the deployment's default
  (``DEFAULT_LANGUAGE``), which is what makes a Persian-facing deployment possible.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from core.config import Settings
from middlewares import user_middleware


class _Pool:
    """Records what the middleware asks the database for."""

    def __init__(self, *, stored: dict[int, Any] | None = None) -> None:
        self.stored = stored or {}
        self.calls: list[tuple[int, str | None, str | None]] = []


def _message(language_code: str | None, user_id: int = 7) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="u", language_code=language_code),
        text="hi",
    )


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        user_middleware,
        "get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    event: Any,
    *,
    stored_language: str | None = None,
    default_language: str = "en",
) -> dict[str, Any]:
    """Run one update through the middleware and return the data it injected."""
    monkeypatch.setattr(
        user_middleware,
        "get_settings",
        lambda: Settings(_env_file=None, DEFAULT_LANGUAGE=default_language),  # type: ignore[call-arg]
    )
    seen: dict[str, Any] = {}

    async def get_or_create_user(
        pool: Any, telegram_id: int, username: str | None, language: str | None = None
    ) -> dict[str, Any]:
        pool.calls.append((telegram_id, username, language))
        return {
            "telegram_id": telegram_id,
            "username": username,
            "language": stored_language if stored_language is not None else language,
        }

    monkeypatch.setattr(user_middleware.database, "get_or_create_user", get_or_create_user)
    pool = _Pool()

    async def handler(target: Any, data: dict[str, Any]) -> str:
        seen.update(data)
        return "handled"

    result = await user_middleware.UserMiddleware()(handler, event, {"pool": pool})
    seen["_result"] = result
    seen["_pool"] = pool
    return seen


async def test_a_persian_client_starts_in_persian(monkeypatch: pytest.MonkeyPatch) -> None:
    data = await _run(monkeypatch, _message("fa-IR"))

    assert data["lang"] == "fa"
    assert data["_pool"].calls == [(7, None, "fa")], "the locale seeds the new row"


async def test_an_english_client_starts_in_english(monkeypatch: pytest.MonkeyPatch) -> None:
    data = await _run(monkeypatch, _message("en-US"))

    assert data["lang"] == "en"
    assert data["_pool"].calls == [(7, None, "en")]


async def test_a_client_in_a_language_we_do_not_speak_gets_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _run(monkeypatch, _message("de-DE"))

    assert data["lang"] == "en"


async def test_a_persian_facing_deployment_defaults_to_persian(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DEFAULT_LANGUAGE=fa`` is what makes a Persian-only product possible without
    touching the code: an unknown locale lands there."""
    data = await _run(monkeypatch, _message("de-DE"), default_language="fa")

    assert data["lang"] == "fa"
    assert data["_pool"].calls == [(7, None, "fa")]


async def test_the_locale_still_wins_over_the_deployment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await _run(monkeypatch, _message("en"), default_language="fa")

    assert data["lang"] == "en", "the client asked for a language we speak"


async def test_a_stored_choice_is_never_overwritten_by_the_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Someone who picked English on a Persian phone must keep English."""
    data = await _run(monkeypatch, _message("fa"), stored_language="en")

    assert data["lang"] == "en"


async def test_a_callback_gets_the_same_treatment(monkeypatch: pytest.MonkeyPatch) -> None:
    callback = CallbackQuery(
        id="1",
        from_user=User(id=7, is_bot=False, first_name="u", language_code="fa"),
        chat_instance="chat",
        data="menu:home",
        message=None,
    )
    seen: dict[str, Any] = {}

    async def get_or_create_user(
        pool: Any, telegram_id: int, username: str | None, language: str | None = None
    ) -> dict[str, Any]:
        return {"telegram_id": telegram_id, "language": language}

    monkeypatch.setattr(user_middleware.database, "get_or_create_user", get_or_create_user)

    async def handler(target: Any, data: dict[str, Any]) -> str:
        seen.update(data)
        return "handled"

    await user_middleware.UserMiddleware()(
        handler, callback, {"pool": _Pool()}
    )

    assert seen["lang"] == "fa"


async def test_an_update_without_a_pool_still_reaches_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware never becomes the reason a handler does not run."""
    seen: dict[str, Any] = {}

    async def handler(target: Any, data: dict[str, Any]) -> str:
        seen.update(data)
        return "handled"

    result = await user_middleware.UserMiddleware()(handler, _message("fa"), {})

    assert result == "handled"
    assert "user" not in seen and "lang" not in seen, "nothing is invented without a pool"


async def test_a_user_updates_their_own_language_and_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of storing it: the *next* update answers in the new language."""
    first = await _run(monkeypatch, _message("en"))
    assert first["lang"] == "en"

    second = await _run(monkeypatch, _message("en"), stored_language="fa")

    assert second["lang"] == "fa"
