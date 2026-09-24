"""Backup & restore under the security audit's four rules.

``OWNER_ID`` is the only key to the feature: with it unset the buttons are
hidden and every action refuses — no admin inherits the privilege by position,
least of all "the first admin". The confirmation button carries nothing but a
short random nonce; the restore itself waits server-side, bound to the owner
and expiring, and is consumed *atomically* — two clicks racing in the same
millisecond run exactly once. The order of operations is the safety story:
consume, build the emergency backup, send it and **await the send**, and only
then open the database transaction — a Telegram call never happens inside one.
A failed send aborts the restore with zero database writes; a sync failure
after the commit is reported as what it is (the restore *is* committed), never
as an impossible rollback. And the key denylist is exact — whole words and
specific compounds, never substrings like ``_at`` or ``key`` — with a recursive
name scan that catches a credential field however deeply a file buries it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, cast

import asyncpg
import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendDocument, SendMessage
from aiogram.types import BufferedInputFile, CallbackQuery, Chat, Document, Message, User

from core.config import Settings
from core.i18n import t
from handlers import admin as admin_module
from services import backup as backup_service

OWNER_ID = 99
OTHER_ADMIN_ID = 100
STRANGER_ID = 7
FA = "fa"
KEY = "intake.stale"  # a real, editable catalogue key

#: What a successful upload hands to the pending store in the flow tests.
SAMPLE = backup_service.Backup(
    texts=((KEY, "en", "New."),),
    state=(("support_contact", "new"),),
    created_at="2026-09-25T12:00:00+00:00",
)


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, serves the uploaded file.

    ``on_call`` observes every method at the moment it is *about to be sent* —
    which is how these tests see the database's state during a Telegram call.
    ``fail_documents`` makes every ``send_document`` raise, standing in for a
    network that will not carry the emergency backup.
    """

    def __init__(
        self,
        download: bytes = b"{}",
        on_call: Callable[[Any], None] | None = None,
        fail_documents: bool = False,
    ) -> None:
        self.calls: list[Any] = []
        self.download_content = download
        self.on_call = on_call
        self.fail_documents = fail_documents

    async def __call__(self, method: Any) -> Any:
        if self.on_call is not None:
            self.on_call(method)
        self.calls.append(method)
        if self.fail_documents and isinstance(method, SendDocument):
            raise RuntimeError("network down")
        return True

    async def download(self, file: Any) -> BytesIO:
        return BytesIO(self.download_content)

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
    def documents(self) -> list[SendDocument]:
        return [call for call in self.calls if isinstance(call, SendDocument)]

    @property
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
            and call.reply_markup is not None
        ]


def _document(size: int = 32) -> Document:
    return Document(file_id="doc", file_unique_id="u", file_size=size)


def _message(
    text: str, bot: RecordingBot, user_id: int = OWNER_ID, *, document: Document | None = None
) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="owner"),
        text=text,
        document=document,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, user_id: int = OWNER_ID) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=User(id=user_id, is_bot=False, first_name="owner"),
        chat_instance="chat",
        data=data,
        message=_message("panel", bot, user_id),
    ).as_(cast(Bot, bot))


def _fsm() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID),
    )


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _raw(**values: Any) -> Settings:
    """``Settings(...)`` spelled with the operator's aliases (``ADMIN_IDS``,
    ``OWNER_ID``) — accepted at runtime, invisible to the typed constructor."""
    return Settings(**values)


def _settings(*, owner: int | None = None) -> Settings:
    """Two admins; ``OWNER_ID`` named — or deliberately absent."""
    values: dict[str, Any] = {"_env_file": None, "ADMIN_IDS": [OWNER_ID, OTHER_ADMIN_ID]}
    if owner is not None:
        values["OWNER_ID"] = owner
    return Settings(**values)


def _nonce_from(keyboard: Any) -> str:
    """The one nonce the preview's confirm button carries."""
    confirm = [data for _, data in _buttons(keyboard) if data.startswith(admin_module.BK_GO)]
    assert len(confirm) == 1
    return confirm[0][len(admin_module.BK_GO) :]


async def _start_and_upload(bot: RecordingBot, state: FSMContext) -> str:
    """Run the flow up to the preview; return the confirmation nonce."""
    await admin_module.on_restore_start(_callback(bot, admin_module.BK_RESTORE), state, lang=FA)
    bot.download_content = json.dumps(SAMPLE.to_dict()).encode("utf-8")
    await admin_module.on_restore_upload(
        _message("file", bot, document=_document()), state, cast(Bot, bot), lang=FA
    )
    return _nonce_from(bot.keyboards[-1])


@pytest.fixture(autouse=True)
def _owner_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OWNER_ID`` is named — and the process env cannot smuggle one in."""
    monkeypatch.delenv("OWNER_ID", raising=False)
    monkeypatch.setattr(admin_module, "get_settings", lambda: _settings(owner=OWNER_ID))


# ---------------------------------------------------------------------------
# Security fix 1 — OWNER_ID: no fallback, the feature is off without it
# ---------------------------------------------------------------------------


def test_without_OWNER_ID_nobody_is_the_owner() -> None:
    # Required test 1 — OWNER_ID unset means disabled for *everyone*.
    unset = _raw(_env_file=None, ADMIN_IDS=[7, 8])
    assert not unset.is_owner(7), "the first admin does NOT inherit the privilege"
    assert not unset.is_owner(8)
    assert not unset.is_owner(None)

    blank = _raw(_env_file=None, ADMIN_IDS=[7, 8], OWNER_ID="")
    assert not blank.is_owner(7)
    assert not blank.is_owner(8)

    zero = _raw(_env_file=None, ADMIN_IDS=[7, 8], OWNER_ID="0")
    assert not zero.is_owner(7)

    named = _raw(_env_file=None, ADMIN_IDS=[7, 8], OWNER_ID="8")
    assert named.is_owner("8")
    assert named.is_owner(8)
    assert not named.is_owner(7)
    assert not named.is_owner(None)


def test_without_OWNER_ID_the_buttons_are_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_module, "get_settings", _settings)  # no owner configured
    destinations = [
        data for _, data in _buttons(admin_module._category_keyboard("en", "cat_system"))
    ]
    assert admin_module.BK_BACKUP not in destinations
    assert admin_module.BK_RESTORE not in destinations
    assert "admin:system" in destinations, "the rest of the category still works"

    monkeypatch.setattr(admin_module, "get_settings", lambda: _settings(owner=OWNER_ID))
    destinations = [
        data for _, data in _buttons(admin_module._category_keyboard("en", "cat_system"))
    ]
    assert admin_module.BK_BACKUP in destinations
    assert admin_module.BK_RESTORE in destinations


async def test_without_OWNER_ID_the_owner_actions_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_module, "get_settings", _settings)  # no owner configured
    state = _fsm()
    bot = RecordingBot()

    await admin_module.on_backup_now(_callback(bot, admin_module.BK_BACKUP), object(), lang=FA)
    await admin_module.on_restore_start(_callback(bot, admin_module.BK_RESTORE), state, lang=FA)
    await admin_module.on_restore_go(
        _callback(bot, f"{admin_module.BK_GO}forged"), state, object(), lang=FA
    )

    assert all(
        answer.show_alert is True
        and t("admin.owner_only", FA) in (answer.text or "")
        for answer in bot.answers
    ), "every action refuses with the same owner-only answer"
    assert not bot.documents
    assert await state.get_state() is None


async def test_only_the_owner_may_back_up(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def build_backup(pool: Any) -> backup_service.Backup:
        called.append("build")
        return backup_service.Backup()

    monkeypatch.setattr(admin_module.backup_service, "build_backup", build_backup)

    for user_id in (OTHER_ADMIN_ID, STRANGER_ID):
        bot = RecordingBot()
        await admin_module.on_backup_now(
            _callback(bot, admin_module.BK_BACKUP, user_id), object(), lang=FA
        )
        assert bot.answers and bot.answers[0].show_alert is True
        assert t("admin.owner_only", FA) in (bot.answers[0].text or "")
    assert called == [], "an admin who is not the owner gets nowhere near the file"


# ---------------------------------------------------------------------------
# The service: what travels, what is judged, what is applied
# ---------------------------------------------------------------------------


class _DB:
    """A tiny transactional stand-in for the two tables a restore touches.

    Writes land in a *staging* copy the transaction either commits or discards
    — which is exactly what these tests need to see: a failure anywhere must
    leave the live tables byte-for-byte as they were, and a Telegram call must
    happen while no staging copy exists at all.
    """

    def __init__(self, texts: dict[tuple[str, str], str], state: dict[str, str]) -> None:
        self.live: dict[str, dict[Any, str]] = {
            "bot_texts": dict(texts),
            "bot_state": dict(state),
        }
        self.staged: dict[str, dict[Any, str]] | None = None
        self.commits = 0
        self.rollbacks = 0
        self.writes = 0
        self.fail_on = ""

    def acquire(self) -> _ConnCtx:
        return _ConnCtx(self)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return _read(self.live, query)

    def target(self) -> dict[str, dict[Any, str]]:
        return self.staged if self.staged is not None else self.live


def _read(tables: dict[str, dict[Any, str]], query: str) -> list[dict[str, Any]]:
    if "SELECT key, lang, value" in query:
        return [
            {"key": key, "lang": lang, "value": value}
            for (key, lang), value in tables["bot_texts"].items()
        ]
    if "SELECT key, value FROM bot_state" in query:
        return [
            {"key": key, "value": value} for key, value in tables["bot_state"].items()
        ]
    raise AssertionError(f"unexpected query: {query}")


class _ConnCtx:
    def __init__(self, db: _DB) -> None:
        self.db = db

    async def __aenter__(self) -> _Conn:
        return _Conn(self.db)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, db: _DB) -> None:
        self.db = db

    def transaction(self) -> _TxnCtx:
        return _TxnCtx(self.db)

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self._guard(query)
        return _read(self.db.target(), query)

    async def execute(self, query: str, *args: Any) -> str:
        self._guard(query)
        self.db.writes += 1
        tables = self.db.target()
        if "DELETE FROM bot_texts" in query:
            tables["bot_texts"].clear()
        elif "DELETE FROM bot_state" in query:
            for key in args[0]:
                tables["bot_state"].pop(key, None)
        else:
            raise AssertionError(f"unexpected query: {query}")
        return "DELETE 0"

    async def executemany(self, query: str, rows: Any) -> None:
        self._guard(query)
        self.db.writes += 1
        tables = self.db.target()
        if "INSERT INTO bot_texts" in query:
            for key, lang, value in rows:
                tables["bot_texts"][(key, lang)] = value
        elif "INSERT INTO bot_state" in query:
            for key, value in rows:
                tables["bot_state"][key] = value
        else:
            raise AssertionError(f"unexpected query: {query}")

    def _guard(self, query: str) -> None:
        if self.db.fail_on and self.db.fail_on in query:
            raise RuntimeError("the database said no")


class _TxnCtx:
    def __init__(self, db: _DB) -> None:
        self.db = db

    async def __aenter__(self) -> _TxnCtx:
        self.db.staged = {name: dict(rows) for name, rows in self.db.live.items()}
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is None:
            self.db.live = self.db.staged or self.db.live
            self.db.commits += 1
        else:
            self.db.rollbacks += 1
        self.db.staged = None
        return False


async def test_a_backup_carries_texts_and_settings_and_never_secrets_or_runtime() -> None:
    db = _DB(
        texts={(KEY, "en"): "Edited.", ("gone.key", "en"): "retired"},
        state={
            "support_contact": "https://t.me/support",
            "youtube_session_server": "http://yt-session-generator:8080",
            "cache_key": "abc123",
            "bot_token": "123:AAA",
            "api_key": "sk",
            "session_id": "s",
            "private_key": "p",
            "cobalt_status": "ready",
            "cobalt_last_use": "2026-01-01T00:00:00+00:00",
            "block_digest_sent_at": "2026-01-01T00:00:00+00:00",
            "helper_down_since:bot": "2026-01-01T00:00:00+00:00",
        },
    )

    snapshot = await backup_service.build_backup(cast(asyncpg.Pool, db))

    assert snapshot.texts == ((KEY, "en", "Edited."),), (
        "the retired key is dropped — what the backup writes, a restore can apply"
    )
    assert snapshot.state == (
        ("cache_key", "abc123"),
        ("support_contact", "https://t.me/support"),
        ("youtube_session_server", "http://yt-session-generator:8080"),
    ), "the settings travel — including the ones a naive denylist would eat"
    blob = json.dumps(snapshot.to_dict()).lower()
    for word in ("token", "api_key", "session_id", "private", "cobalt", "digest", "helper"):
        assert word not in blob, word
    # What it writes is what a restore reads back — by construction.
    assert backup_service.validate_backup(snapshot.to_dict()) == snapshot


def test_the_denylist_is_exact_not_substring() -> None:
    # Required test 4's validation half — substrings like `_at` / `key` would
    # destroy valid configuration, so whole words and compounds decide.
    safe = [
        "key",
        "keyboard",
        "cache_key",
        "author",  # "auth" as a substring, not as a compound
        "youtube_session_server",  # "session", not "session id"
        "menu_auto_best",
        "support_contact",
        "platform",
        "translator",
        "stat_atlas",  # contains "_at" mid-key; only a *suffix* stamp is runtime
    ]
    for key in safe:
        assert backup_service.is_backup_key(key), key

    refused = [
        "bot_token",
        "webhook_secret",
        "api_key",
        "private_key",
        "access_key",
        "auth_key",
        "session_id",
        "oauth_token",
        "otp",
        "authorization",
        "passwords",
        "cobalt_status",
        "cobalt_last_use",
        "block_digest_sent_at",
        "helper_down_since:bot",
        "helper_alerted_at:bot",
    ]
    for key in refused:
        assert not backup_service.is_backup_key(key), key

    # …and a file full of the safe words is judged safe, not shredded.
    good = {
        "schema_version": backup_service.SCHEMA_VERSION,
        "created_at": "",
        "bot_texts": [],
        "bot_state": {key: "x" for key in safe},
    }
    assert backup_service.validate_backup(good).state == tuple(sorted((key, "x") for key in safe))


def test_an_uploaded_file_is_judged_before_it_is_trusted() -> None:
    good = {
        "schema_version": backup_service.SCHEMA_VERSION,
        "created_at": "2026-09-25T12:00:00+00:00",
        "bot_texts": [{"key": KEY, "lang": "en", "value": "Edited."}],
        "bot_state": {"support_contact": "https://t.me/support"},
    }
    assert backup_service.validate_backup(good).state == (
        ("support_contact", "https://t.me/support"),
    )

    def reason(data: object) -> str:
        with pytest.raises(backup_service.BackupError) as excinfo:
            backup_service.validate_backup(data)
        return excinfo.value.reason

    assert reason({**good, "schema_version": 99}) == "schema"
    assert reason({**good, "bot_token": "123:AAA"}) == "prohibited", "unknown field"
    assert reason({**good, "bot_state": {"api_key": "x"}}) == "prohibited", "a credential"
    assert reason({**good, "bot_state": {"cobalt_status": "ready"}}) == "prohibited", (
        "a generated runtime cache"
    )
    assert (
        reason({**good, "bot_texts": [{"key": KEY, "lang": "en", "value": "x", "token": "y"}]})
        == "prohibited"
    )
    assert reason({**good, "bot_texts": [{"key": KEY, "lang": "en"}]}) == "format"
    assert reason({**good, "bot_texts": ["x"]}) == "format"
    assert reason({**good, "bot_texts": [{"key": "gone.key", "lang": "en", "value": "x"}]}) == "locked"
    assert reason({**good, "bot_state": {"": "x"}}) == "format"
    assert reason({**good, "created_at": 5}) == "format"
    assert reason({"schema_version": backup_service.SCHEMA_VERSION}) == "format"
    assert reason(["not", "a", "dict"]) == "format"


def test_nested_secrets_are_caught_however_deep() -> None:
    # Required test 5 — a credential-named field anywhere in the tree refuses
    # the whole file, however deeply it is buried.
    def reason(data: object) -> str:
        with pytest.raises(backup_service.BackupError) as excinfo:
            backup_service.validate_backup(data)
        return excinfo.value.reason

    shell = {
        "schema_version": backup_service.SCHEMA_VERSION,
        "created_at": "",
        "bot_texts": [],
        "bot_state": {},
    }
    assert (
        reason({**shell, "meta": {"api_key": "x"}}) == "prohibited"
    ), "one level down"
    assert (
        reason(
            {
                **shell,
                "bot_texts": [
                    {"key": KEY, "lang": "en", "value": "x", "meta": {"deep": [{"password": "p"}]}}
                ],
            }
        )
        == "prohibited"
    ), "inside a row, inside a dict, inside a list"
    assert (
        reason({**shell, "history": [[{"auth": {"tokens": ["t"]}}]]}) == "prohibited"
    ), "through lists of lists of dicts"
    assert (
        reason({**shell, "meta": {"a": {"b": {"c": {"session_id": "s"}}}}}) == "prohibited"
    ), "four levels down"


def test_the_secret_scan_is_recursive_and_judges_names_not_values() -> None:
    scan = backup_service._find_secret_field
    assert scan({"a": {"b": [{"c": [{"password": "x"}]}]}}) == "password"
    assert scan([{"deep": {"deeper": {"session_id": "s"}}}]) == "session_id"
    assert scan({"history": [[{"auth": {"tokens": ["t"]}}]]}) == "tokens"
    assert (
        scan({"schema_version": 1, "bot_texts": [{"key": KEY, "value": "the word token"}]}) is None
    ), "values are user content — only field *names* decide"
    assert scan({"note": "key token password secret"}) is None


async def test_a_restore_replaces_the_configuration_and_never_the_runtime_state() -> None:
    db = _DB(
        texts={(KEY, "en"): "old", ("menu.back", "en"): "old too"},
        state={"support_contact": "old", "cobalt_status": "ready", "stale_setting": "x"},
    )
    backup = backup_service.Backup(
        texts=((KEY, "en", "new"),),
        state=(("support_contact", "new"),),
        created_at="2026-09-25T12:00:00+00:00",
    )

    await backup_service.apply_backup(cast(asyncpg.Pool, db), backup)

    assert db.live["bot_texts"] == {(KEY, "en"): "new"}, "the texts go back to the snapshot"
    assert db.live["bot_state"] == {
        "support_contact": "new",
        "cobalt_status": "ready",
    }, "settings restored, a stale setting dropped, runtime state untouched"
    assert (db.commits, db.rollbacks) == (1, 0)


async def test_a_failure_mid_restore_rolls_the_whole_thing_back() -> None:
    db = _DB(
        texts={(KEY, "en"): "old"},
        state={"support_contact": "old"},
    )
    db.fail_on = "INSERT INTO bot_state"
    backup = backup_service.Backup(
        texts=((KEY, "en", "new"),),
        state=(("support_contact", "new"),),
    )

    with pytest.raises(RuntimeError):
        await backup_service.apply_backup(cast(asyncpg.Pool, db), backup)

    assert db.live == {
        "bot_texts": {(KEY, "en"): "old"},
        "bot_state": {"support_contact": "old"},
    }, "nothing is applied — the texts section rolls back with the settings section"
    assert (db.commits, db.rollbacks) == (0, 1)


# ---------------------------------------------------------------------------
# Security fix 2 — the confirmation is a nonce; consumption is atomic
# ---------------------------------------------------------------------------


async def test_the_confirmation_carries_only_a_nonce() -> None:
    bot = RecordingBot()
    state = _fsm()

    nonce = await _start_and_upload(bot, state)

    keyboard = bot.keyboards[-1]
    confirm = [data for _, data in _buttons(keyboard) if data.startswith(admin_module.BK_GO)]
    assert confirm == [f"{admin_module.BK_GO}{nonce}"]
    assert len(confirm[0]) <= 64, "Telegram's callback-data cap holds"
    assert nonce and nonce not in ("restore", "backup"), "a nonce, not a name"
    assert SAMPLE.to_dict()["bot_texts"] != nonce, "the payload is not in the button"
    assert "restore" not in await state.get_data(), "and not in the FSM either"
    assert t("admin.restore_preview", FA, texts=1, settings=1, created=SAMPLE.created_at) in (
        bot.screens
    )
    # The nonce is what runs it — and only once, as the other tests prove.
    assert backup_service.take_pending(nonce, owner=OWNER_ID) == SAMPLE


def test_an_expired_confirmation_is_rejected() -> None:
    # Required test 7 — the payload's TTL is strict.
    parsed = backup_service.Backup(state=(("support_contact", "new"),))

    alive = backup_service.remember_pending(parsed, owner=OWNER_ID, now=100.0)
    assert (
        backup_service.take_pending(
            alive, owner=OWNER_ID, now=100.0 + backup_service.PENDING_TTL_S - 1
        )
        == parsed
    ), "inside the TTL it runs"

    stale = backup_service.remember_pending(parsed, owner=OWNER_ID, now=100.0)
    assert (
        backup_service.take_pending(
            stale, owner=OWNER_ID, now=100.0 + backup_service.PENDING_TTL_S
        )
        is None
    ), "at the TTL it is gone"


async def test_an_expired_confirmation_is_rejected_in_the_flow() -> None:
    db = _DB(texts={}, state={})
    bot = RecordingBot()
    state = _fsm()
    gone = backup_service.remember_pending(SAMPLE, owner=OWNER_ID, now=-1000.0)

    await admin_module.on_restore_go(
        _callback(bot, f"{admin_module.BK_GO}{gone}"),
        state,
        cast(asyncpg.Pool, db),
        lang=FA,
    )

    assert bot.answers[-1].text == t("admin.restore_expired", FA)
    assert bot.answers[-1].show_alert is True
    assert not bot.documents, "no emergency backup is even built"
    assert (db.writes, db.commits, db.rollbacks) == (0, 0, 0)


def test_a_strangers_tap_cannot_spend_or_destroy_a_pending_restore() -> None:
    parsed = backup_service.Backup(state=(("support_contact", "new"),))
    nonce = backup_service.remember_pending(parsed, owner=OWNER_ID)

    assert backup_service.take_pending(nonce, owner=OTHER_ADMIN_ID) is None
    assert backup_service.take_pending(nonce, owner=STRANGER_ID) is None
    assert (
        backup_service.take_pending(nonce, owner=OWNER_ID) == parsed
    ), "the stranger's tap leaves the owner's confirmation alone"

    doomed = backup_service.remember_pending(parsed, owner=OWNER_ID)
    backup_service.drop_pending(doomed, owner=OTHER_ADMIN_ID)
    assert (
        backup_service.take_pending(doomed, owner=OWNER_ID) == parsed
    ), "and a stranger cannot cancel it either"

    gone = backup_service.remember_pending(parsed, owner=OWNER_ID)
    backup_service.drop_pending(gone, owner=OWNER_ID)
    assert backup_service.take_pending(gone, owner=OWNER_ID) is None


async def test_a_confirmation_from_another_admin_is_rejected() -> None:
    # Required test 3 — only the owner's click can spend the pending restore.
    db = _DB(texts={}, state={})
    bot = RecordingBot()
    state = _fsm()
    nonce = backup_service.remember_pending(SAMPLE, owner=OWNER_ID)

    for user_id in (OTHER_ADMIN_ID, STRANGER_ID):
        await admin_module.on_restore_go(
            _callback(bot, f"{admin_module.BK_GO}{nonce}", user_id),
            state,
            cast(asyncpg.Pool, db),
            lang=FA,
        )

    assert all(
        answer.show_alert is True and t("admin.owner_only", FA) in (answer.text or "")
        for answer in bot.answers
    )
    assert (db.writes, db.commits, db.rollbacks) == (0, 0, 0)
    assert not bot.documents
    assert (
        backup_service.take_pending(nonce, owner=OWNER_ID) == SAMPLE
    ), "the owner's confirmation survives everyone else's taps"


# ---------------------------------------------------------------------------
# Security fix 3 — the order: consume, send, await, and only then transact
# ---------------------------------------------------------------------------


async def test_the_restore_runs_in_the_audit_order(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    bot = RecordingBot()

    async def build_backup(pool: Any) -> backup_service.Backup:
        events.append("build")
        return backup_service.Backup(state=(("support_contact", "old"),))

    async def apply_backup(pool: Any, parsed: backup_service.Backup) -> None:
        events.append(("apply", len(bot.documents)))

    async def restored(rows: Any) -> int:
        events.append("sync")
        return 0

    async def text_overrides(pool: Any) -> list[Any]:
        return []

    monkeypatch.setattr(admin_module.backup_service, "build_backup", build_backup)
    monkeypatch.setattr(admin_module.backup_service, "apply_backup", apply_backup)
    monkeypatch.setattr(admin_module.text_store, "restored", restored)
    monkeypatch.setattr(admin_module.database, "text_overrides", text_overrides)

    state = _fsm()
    nonce = await _start_and_upload(bot, state)
    assert events == [], "a preview is not a confirmation — nothing has happened yet"

    await admin_module.on_restore_go(
        _callback(bot, f"{admin_module.BK_GO}{nonce}"), state, object(), lang=FA
    )

    assert events == ["build", ("apply", 1), "sync"], (
        "consume → build → send (already delivered when apply runs) → "
        "transaction → post-commit sync"
    )
    assert bot.documents[0].caption == t("admin.restore_emergency_caption", FA)
    assert t("admin.restore_done", FA, texts=1, settings=1) in bot.screens
    assert await state.get_state() is None, "the flow ends — cleanly"


async def test_a_failed_emergency_send_aborts_with_zero_database_writes() -> None:
    # Required test 2 — if the document cannot be sent, nothing is touched.
    db = _DB(texts={(KEY, "en"): "old"}, state={"support_contact": "old"})
    bot = RecordingBot(fail_documents=True)
    state = _fsm()
    nonce = await _start_and_upload(bot, state)

    await admin_module.on_restore_go(
        _callback(bot, f"{admin_module.BK_GO}{nonce}"),
        state,
        cast(asyncpg.Pool, db),
        lang=FA,
    )

    assert t("admin.restore_aborted", FA, detail="network down") in bot.screens
    assert len(bot.documents) == 1, "the emergency send was attempted"
    assert (db.writes, db.commits, db.rollbacks) == (0, 0, 0), "zero database writes"
    assert db.live == {
        "bot_texts": {(KEY, "en"): "old"},
        "bot_state": {"support_contact": "old"},
    }
    assert not any(
        t("admin.restore_done", FA, texts=1, settings=1) in screen for screen in bot.screens
    )
    assert backup_service.take_pending(nonce, owner=OWNER_ID) is None, (
        "consumption happens before the send by design — the nonce is spent, "
        "the restore is not"
    )


async def test_the_telegram_api_runs_outside_the_database_transaction() -> None:
    # Required test 4 — no network call may ever see an open transaction.
    db = _DB(texts={(KEY, "en"): "old"}, state={"support_contact": "old"})
    api_checks: list[tuple[str, bool, int, int]] = []

    def observe(method: Any) -> None:
        # ``(call, transaction-not-open, writes so far, commits so far)``.
        api_checks.append(
            (type(method).__name__, db.staged is None, db.writes, db.commits)
        )

    upload_bot = RecordingBot()
    state = _fsm()
    nonce = await _start_and_upload(upload_bot, state)
    go_bot = RecordingBot(on_call=observe)

    async def restored(rows: Any) -> int:
        return 0

    async def text_overrides(pool: Any) -> list[Any]:
        return []

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(admin_module.text_store, "restored", restored)
        monkeypatch.setattr(admin_module.database, "text_overrides", text_overrides)
        await admin_module.on_restore_go(
            _callback(go_bot, f"{admin_module.BK_GO}{nonce}"),
            state,
            cast(asyncpg.Pool, db),
            lang=FA,
        )
    finally:
        monkeypatch.undo()

    assert api_checks, "the restore did make its calls"
    assert all(
        outside for _, outside, _, _ in api_checks
    ), f"an API call ran inside the transaction: {api_checks}"
    sends = [check for check in api_checks if check[0] == "SendDocument"]
    assert sends == [("SendDocument", True, 0, 0)], (
        "the emergency send saw a closed transaction, no writes, no commits yet"
    )
    assert db.commits == 1, "the transaction does happen — strictly afterwards"
    assert db.rollbacks == 0
    assert db.live["bot_state"] == {"support_contact": "new"}


async def test_a_sync_failure_after_commit_reports_the_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    # Required test 6 — the restore is already committed; say so, never
    # "rolled back" — that cannot happen and must not be claimed.
    db = _DB(texts={(KEY, "en"): "old"}, state={"support_contact": "old"})
    bot = RecordingBot()

    async def restored(rows: Any) -> int:
        raise RuntimeError("channel gone")

    async def text_overrides(pool: Any) -> list[Any]:
        return []

    monkeypatch.setattr(admin_module.text_store, "restored", restored)
    monkeypatch.setattr(admin_module.database, "text_overrides", text_overrides)

    state = _fsm()
    nonce = await _start_and_upload(bot, state)
    await admin_module.on_restore_go(
        _callback(bot, f"{admin_module.BK_GO}{nonce}"),
        state,
        cast(asyncpg.Pool, db),
        lang=FA,
    )

    assert t("admin.restore_sync_failed", FA, detail="channel gone") in bot.screens
    assert (db.commits, db.rollbacks) == (1, 0), "committed — and no rollback was attempted"
    assert db.live["bot_state"] == {"support_contact": "new"}, "the restore really is applied"
    assert t("admin.restore_failed", FA, detail="channel gone") not in bot.screens, (
        "no rollback language — the data is in"
    )
    assert t("admin.restore_done", FA, texts=1, settings=1) not in bot.screens


async def test_a_failed_restore_changes_nothing_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def build_backup(pool: Any) -> backup_service.Backup:
        return backup_service.Backup()

    async def apply_backup(pool: Any, parsed: backup_service.Backup) -> None:
        raise RuntimeError("boom")

    async def restored(rows: Any) -> int:
        events.append("restored")
        return 0

    async def text_overrides(pool: Any) -> list[Any]:
        return []

    monkeypatch.setattr(admin_module.backup_service, "build_backup", build_backup)
    monkeypatch.setattr(admin_module.backup_service, "apply_backup", apply_backup)
    monkeypatch.setattr(admin_module.text_store, "restored", restored)
    monkeypatch.setattr(admin_module.database, "text_overrides", text_overrides)

    bot = RecordingBot()
    state = _fsm()
    nonce = await _start_and_upload(bot, state)
    await admin_module.on_restore_go(
        _callback(bot, f"{admin_module.BK_GO}{nonce}"), state, object(), lang=FA
    )

    assert t("admin.restore_failed", FA, detail="boom") in bot.screens
    assert events == [], "the texts are never synced on a failed restore"
    assert await state.get_state() is None


async def test_two_concurrent_confirmations_run_exactly_one_restore() -> None:
    # Required test 8 — atomic consumption: two clicks in the same millisecond,
    # exactly one restore.
    db = _DB(texts={(KEY, "en"): "old"}, state={"support_contact": "old"})
    bot = RecordingBot()
    state = _fsm()
    nonce = await _start_and_upload(bot, state)

    async def restored(rows: Any) -> int:
        return 0

    async def text_overrides(pool: Any) -> list[Any]:
        return []

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(admin_module.text_store, "restored", restored)
        monkeypatch.setattr(admin_module.database, "text_overrides", text_overrides)
        first = _callback(bot, f"{admin_module.BK_GO}{nonce}")
        second = _callback(bot, f"{admin_module.BK_GO}{nonce}")
        await asyncio.gather(
            admin_module.on_restore_go(
                first, state, cast(asyncpg.Pool, db), lang=FA
            ),
            admin_module.on_restore_go(
                second, state, cast(asyncpg.Pool, db), lang=FA
            ),
        )
    finally:
        monkeypatch.undo()

    assert (db.commits, db.rollbacks) == (1, 0), "exactly one restore ran"
    assert len(bot.documents) == 1, "exactly one emergency backup was sent"
    assert (
        bot.screens.count(t("admin.restore_done", FA, texts=1, settings=1)) == 1
    ), "one confirmation won"
    assert (
        sum(1 for answer in bot.answers if answer.text == t("admin.restore_expired", FA)) == 1
    ), "the other found nothing to consume"
    assert backup_service.take_pending(nonce, owner=OWNER_ID) is None


# ---------------------------------------------------------------------------
# The panel flow: files, refusals, cancel
# ---------------------------------------------------------------------------


async def test_the_backup_is_a_portable_json_file(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = backup_service.Backup(
        texts=((KEY, "en", "Edited."),),
        state=(("support_contact", "https://t.me/support"),),
        created_at="2026-09-25T12:00:00+00:00",
    )

    async def build_backup(pool: Any) -> backup_service.Backup:
        return snapshot

    monkeypatch.setattr(admin_module.backup_service, "build_backup", build_backup)
    bot = RecordingBot()

    await admin_module.on_backup_now(_callback(bot, admin_module.BK_BACKUP), object(), lang=FA)

    assert len(bot.documents) == 1
    sent = bot.documents[0]
    file = cast(BufferedInputFile, sent.document)
    assert file.filename == "bot-backup-20260925-1200.json"
    assert json.loads(file.data) == snapshot.to_dict()
    assert sent.caption == t("admin.backup_caption", FA)


async def test_an_untrusted_file_is_refused_before_it_can_change_anything() -> None:
    hostile = {
        "schema_version": backup_service.SCHEMA_VERSION,
        "created_at": "",
        "bot_texts": [],
        "bot_state": {"bot_token": "123:AAA"},
    }
    bot = RecordingBot(download=json.dumps(hostile).encode("utf-8"))
    state = _fsm()

    await admin_module.on_restore_upload(
        _message("file", bot, document=_document()), state, cast(Bot, bot), lang=FA
    )

    assert t("admin.restore_invalid", FA, reason="prohibited") in bot.screens
    assert "restore" not in await state.get_data(), "nothing is kept for a confirm step"
    assert all(
        not data.startswith(admin_module.BK_GO) for _, data in _buttons(bot.keyboards[-1])
    ), "and there is no confirm button to press"
    assert not bot.documents


async def test_cancel_drops_the_pending_restore() -> None:
    bot = RecordingBot()
    state = _fsm()
    nonce = await _start_and_upload(bot, state)

    await admin_module.on_restore_cancel(
        _callback(bot, f"{admin_module.BK_CANCEL}:{nonce}"), state, lang=FA
    )

    assert backup_service.take_pending(nonce, owner=OWNER_ID) is None
    assert await state.get_state() is None
    assert t("admin.restore_cancelled", FA) in bot.screens

    # …and a stranger's tap can neither cancel nor spend it.
    other_bot = RecordingBot()
    other_state = _fsm()
    other_nonce = await _start_and_upload(other_bot, other_state)
    await admin_module.on_restore_cancel(
        _callback(other_bot, f"{admin_module.BK_CANCEL}:{other_nonce}", OTHER_ADMIN_ID),
        other_state,
        lang=FA,
    )
    assert t("admin.owner_only", FA) in (other_bot.answers[-1].text or "")
    assert backup_service.take_pending(other_nonce, owner=OWNER_ID) == SAMPLE


async def test_a_message_during_the_upload_step_asks_again() -> None:
    bot = RecordingBot()

    await admin_module.on_restore_reprompt(_message("hello", bot), lang=FA)

    assert t("admin.restore_prompt", FA) in bot.screens
