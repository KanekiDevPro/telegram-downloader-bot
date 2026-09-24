"""The editable texts: who may edit what, what a replacement may look like, and
what saving one actually does.

The contract pinned here is the safety-critical one: only admins get near the
editor, only user-facing keys are editable (operator screens and the command
menu are locked), a replacement may not smuggle in a new placeholder (nor the
``{x.__class__}`` attribute trick), unbalanced or unsupported markup is refused
at the desk instead of breaking sends in a chat, every save is validated before
it is stored, previews prove the text sends byte-for-byte, resets return the
catalogue default, and every change leaves an audit row. Offline throughout —
the database is a recorder.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User

from core import texts as text_store
from core.catalog import MESSAGES
from core.i18n import t
from handlers import admin as admin_module

ADMIN_ID = 99
STRANGER_ID = 7
KEY = "intake.probe_failed"


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def screens(self) -> list[str]:
        return [
            call.text or ""
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
        ]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    @property
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
            and call.reply_markup is not None
        ]


def _message(text: str, bot: RecordingBot, user_id: int = ADMIN_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, user_id: int = ADMIN_ID) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        chat_instance="chat",
        data=data,
        message=_message("texts", bot, user_id),
    ).as_(cast(Bot, bot))


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


class _TextDB:
    """``bot_texts`` + the audit trail, in memory — what the editor writes."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        self.audit: list[tuple[str, str]] = []

    async def set_text_override(
        self, pool: Any, key: str, lang: str, value: str, updated_by: int = 0
    ) -> None:
        self.rows[(key, lang)] = value

    async def reset_text_override(self, pool: Any, key: str, lang: str) -> None:
        self.rows.pop((key, lang), None)

    async def record_fix_event(self, pool: Any, *, kind: str, detail: str = "") -> None:
        self.audit.append((kind, detail))


@pytest.fixture(autouse=True)
def _desk(monkeypatch: pytest.MonkeyPatch) -> _TextDB:
    """Admin rights, an in-memory database, and a clean override layer per test."""

    def settings() -> Any:
        from core.config import Settings

        return Settings(_env_file=None, ADMIN_IDS=str(ADMIN_ID))  # type: ignore[call-arg, arg-type]

    db = _TextDB()
    monkeypatch.setattr(admin_module, "get_settings", settings)
    monkeypatch.setattr(admin_module.database, "set_text_override", db.set_text_override)
    monkeypatch.setattr(admin_module.database, "reset_text_override", db.reset_text_override)
    monkeypatch.setattr(admin_module.database, "record_fix_event", db.record_fix_event)
    text_store.apply_overrides([])
    return db


async def _open_editor(bot: RecordingBot, state: FSMContext, data: str, *, key: str = KEY) -> None:
    await admin_module.on_texts_button(_callback(bot, data), state, object(), lang="en")


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


async def test_a_stranger_cannot_reach_any_texts_action(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()

    for data in (f"{admin_module.TXT_PREFIX}cat:intake", f"{admin_module.TXT_PREFIX}key:{KEY}"):
        await admin_module.on_texts_button(
            _callback(bot, data, STRANGER_ID), state, object(), lang="en"
        )

    assert all(answer.show_alert for answer in bot.answers)
    assert bot.screens == [], "nothing is rendered"
    assert _desk.rows == {} and _desk.audit == []

    await admin_module.on_text_edit_value(
        _message("hacked", bot, STRANGER_ID), state, object(), lang="en"
    )
    assert _desk.rows == {}, "and a typed value is stored nowhere"


async def test_a_locked_text_is_not_editable_even_by_an_admin(_desk: _TextDB) -> None:
    """Operator screens and the command menu are nobody's to replace from here —
    a crafted payload naming one gets the honest refusal."""
    bot = RecordingBot()
    state = _fsm()

    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}edit:en:admin.title")
    assert await state.get_state() is None
    assert bot.answers and bot.answers[-1].show_alert is True
    assert _desk.rows == {}

    assert not text_store.editable("admin.title")
    assert not text_store.editable("cmd.start")
    assert text_store.editable(KEY)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason"),
    (
        ("x" * (text_store.MAX_TEXT_LENGTH + 1), "length"),
        ("Hello {name} {secret}", "placeholder"),
        ("Hello {name.__class__}", "placeholder"),
        ("Hello {name", "placeholder"),
        ("Hello <b>name</i>", "markup"),
        ("Hello <br/>name", "markup"),
        ('Click <a href="javascript:alert(1)">here</a>', "markup"),
    ),
)
def test_an_invalid_replacement_names_why_it_was_refused(value: str, reason: str) -> None:
    assert text_store.validate_text("start.welcome", value) == reason


def test_a_valid_replacement_is_accepted() -> None:
    assert text_store.validate_text("start.welcome", "Hi <b>{name}</b> 👋") is None
    assert text_store.validate_text("start.welcome", 'Read <a href="https://x.example">this</a>') is None
    assert text_store.validate_text("start.welcome", "No markup at all, no placeholders") is None


async def test_a_refused_edit_is_not_stored_and_says_why(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()
    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}edit:en:{KEY}")
    assert await state.get_state() == admin_module.AdminStates.text_edit.state

    await admin_module.on_text_edit_value(_message("Hi {oops}", bot), state, object(), lang="en")

    assert _desk.rows == {} and _desk.audit == []
    assert await state.get_state() is None, "the flow ends — no half-saved step"
    assert any("Not saved" in text for text in bot.screens)


# ---------------------------------------------------------------------------
# Persistence, preview, reset
# ---------------------------------------------------------------------------


async def test_a_saved_edit_is_validated_proven_and_audited(_desk: _TextDB) -> None:
    """The flow: typed value → validated → sent byte-for-byte (the preview is
    the proof it sends) → stored → live — with an audit row behind it."""
    bot = RecordingBot()
    state = _fsm()
    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}edit:en:{KEY}")

    await admin_module.on_text_edit_value(
        _message("Could not read this link — try again.", bot), state, object(), lang="en"
    )

    assert "Could not read this link — try again." in bot.texts, (
        "the preview is the exact text users will receive"
    )
    assert _desk.rows[(KEY, "en")] == "Could not read this link — try again."
    assert text_store.override_for(KEY, "en") == "Could not read this link — try again."
    assert t(KEY, "en") == "Could not read this link — try again."
    assert t(KEY, "fa") == MESSAGES[KEY]["fa"], "the other language is untouched"
    # The audit row carries actor, key, language (the row's own created_at is
    # the timestamp) and the before/after values — ``null`` = catalogue default.
    assert [kind for kind, _detail in _desk.audit] == ["text_override"]
    assert json.loads(_desk.audit[0][1]) == {
        "action": "set",
        "key": KEY,
        "lang": "en",
        "actor": ADMIN_ID,
        "before": None,
        "after": "Could not read this link — try again.",
    }
    assert "Saved" in bot.screens[-1]


async def test_reset_returns_the_catalogue_default_and_is_audited(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()
    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}edit:en:{KEY}")
    await admin_module.on_text_edit_value(_message("A replacement", bot), state, object(), lang="en")

    await admin_module.on_texts_button(
        _callback(bot, f"{admin_module.TXT_PREFIX}reset:en:{KEY}"), state, object(), lang="en"
    )

    assert (KEY, "en") not in _desk.rows
    assert text_store.override_for(KEY, "en") is None
    assert t(KEY, "en") == MESSAGES[KEY]["en"]
    kind, detail = _desk.audit[-1]
    assert kind == "text_reset"
    assert json.loads(detail) == {
        "action": "reset",
        "key": KEY,
        "lang": "en",
        "actor": ADMIN_ID,
        "before": "A replacement",
        "after": None,
    }, "the reset row remembers what it removed — and what replaced it (nothing)"
    assert "back to its default" in bot.screens[-1]


# ---------------------------------------------------------------------------
# Concurrency: last-write-wins, and nothing lost from the history
# ---------------------------------------------------------------------------


async def test_concurrent_edits_are_last_write_wins_and_never_lose_history(
    _desk: _TextDB,
) -> None:
    """Two admins open the same text and both type before either saves — the
    classic lost-update setup. The contract (stated in core.texts and
    handlers.admin._text_audit): **last write wins** the present value, and
    every write keeps its own audit row with its own before/after pair — so the
    overwritten value is never lost from the *history*, only from the present."""
    bot_a, bot_b = RecordingBot(), RecordingBot()
    state_a, state_b = _fsm(), _fsm()
    await _open_editor(bot_a, state_a, f"{admin_module.TXT_PREFIX}edit:en:{KEY}")
    await _open_editor(bot_b, state_b, f"{admin_module.TXT_PREFIX}edit:en:{KEY}")

    await admin_module.on_text_edit_value(
        _message("the first editor's text", bot_a), state_a, object(), lang="en"
    )
    await admin_module.on_text_edit_value(
        _message("the second editor's text", bot_b), state_b, object(), lang="en"
    )

    assert _desk.rows[(KEY, "en")] == "the second editor's text", "last write wins"
    details = [json.loads(detail) for _kind, detail in _desk.audit]
    assert [d["after"] for d in details] == [
        "the first editor's text",
        "the second editor's text",
    ]
    assert [d["before"] for d in details] == [None, "the first editor's text"], (
        "each row remembers exactly what it overwrote — the chain is the proof"
    )
    assert all(d["actor"] == ADMIN_ID and d["key"] == KEY and d["lang"] == "en" for d in details)


async def test_the_preview_shows_the_text_as_users_would_receive_it(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()

    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}preview:fa:{KEY}")

    assert MESSAGES[KEY]["fa"] in bot.texts, "the raw template, placeholders and all"
    assert _desk.rows == {}, "a preview stores nothing"


async def test_each_stale_payload_is_answered_never_followed(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()

    for data in (
        f"{admin_module.TXT_PREFIX}cat:nope",
        f"{admin_module.TXT_PREFIX}key:no.such.key",
        f"{admin_module.TXT_PREFIX}zzz",
        f"{admin_module.TXT_PREFIX}edit:xx:{KEY}",
    ):
        await admin_module.on_texts_button(_callback(bot, data), state, object(), lang="en")

    assert len(bot.answers) == 4
    assert all(answer.show_alert for answer in bot.answers)
    assert await state.get_state() is None, "no half-open editor step"
    assert _desk.rows == {}


# ---------------------------------------------------------------------------
# The browser itself: categories, paging, the key screen
# ---------------------------------------------------------------------------


async def test_the_editor_groups_texts_by_feature(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()

    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}cat:media")

    keys = text_store.category_keys("media")
    assert any(key.startswith("media.") for key in keys), "the media texts"
    assert any(key.startswith("audio.") for key in keys), "and the audio prompts"
    assert any(key.startswith("fmt.") for key in keys), "and the format buttons"
    labels = [label for label, _ in _buttons(bot.keyboards[-1]) if "." in label]
    assert labels and all(label in keys for label in labels), (
        "the page lists this category's keys and only those"
    )
    assert all(not label.startswith("admin.") for label in labels), (
        "operator screens are locked out of the editor"
    )


async def test_a_long_category_pages_and_never_dumps_every_key(_desk: _TextDB) -> None:
    keys = text_store.category_keys("media")
    assert len(keys) > admin_module.TEXTS_PAGE_SIZE, "this category really does page"

    _, keyboard = admin_module._texts_keys_screen("media", 0, "en")
    destinations = dict(_buttons(keyboard))
    assert destinations["▶️ Next"] == "txt:keys:media:1"
    assert destinations["⬅️ Back"] == "admin:texts"
    assert destinations["🏠 Home"] == "menu:home"

    shown = [
        text
        for text, _ in _buttons(keyboard)
        if "." in text and not text.startswith("▶")
    ]
    assert len(shown) <= admin_module.TEXTS_PAGE_SIZE


async def test_the_key_screen_edits_per_language_and_marks_edits(_desk: _TextDB) -> None:
    bot = RecordingBot()
    state = _fsm()
    await _open_editor(bot, state, f"{admin_module.TXT_PREFIX}edit:fa:{KEY}")
    await admin_module.on_text_edit_value(_message("یک متن تازه", bot), state, object(), lang="en")

    text, keyboard = admin_module._text_key_screen(KEY, "en")

    destinations = dict(_buttons(keyboard))
    assert destinations["✏️ Edit EN"] == f"txt:edit:en:{KEY}"
    assert destinations["✏️ Edit FA"] == f"txt:edit:fa:{KEY}"
    assert destinations["🔄 Reset FA"] == f"txt:reset:fa:{KEY}", (
        "a reset button exists only for a language that was edited"
    )
    assert f"txt:reset:en:{KEY}" not in destinations.values()
    assert "یک متن تازه" in text, "the current values are what the screen shows"


def test_every_texts_button_has_somewhere_to_go() -> None:
    """The browser's own destinations, all templated — the namespace must be
    handled so no button can dead-end."""
    import inspect

    source = inspect.getsource(admin_module)
    offered = {data for _, data in _buttons(admin_module._texts_screen("en")[1])}
    offered |= {data for _, data in _buttons(admin_module._texts_keys_screen("intake", 0, "en")[1])}
    offered |= {data for _, data in _buttons(admin_module._text_key_screen(KEY, "en")[1])}
    for data in sorted(offered):
        namespace = data.split(":", 1)[0]
        assert f'"{namespace}:' in source or f'"{data}"' in source, data
