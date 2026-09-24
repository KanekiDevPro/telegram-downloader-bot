"""Portable backups: the bot's *configuration*, and nothing else.

What travels is what an operator edited — the custom texts (``bot_texts``) and
the settings rows (``bot_state``) — plus the two facts a restore must judge the
file by: the backup format's ``schema_version`` and when it was taken. What
never travels is just as deliberate: tokens, API keys, credentials and cookies
are refused by *key*, and so is the generated runtime state (health stamps,
last-use markers, alert times) — a backup is the configuration, not the machine
it ran on. The same rule guards both doors, so a file built here always passes
the restore's validation, and a file naming anything the backup would never
write is refused before it can touch the database.

The confirmation is a short random nonce into a server-side, owner-bound,
expiring store (:func:`remember_pending` / :func:`take_pending`) — the payload
never travels in callback data, and consumption is atomic: two clicks race,
exactly one wins. And the order of operations is the safety story: the
emergency backup is *sent and confirmed* before the transaction opens (no
network call ever happens inside it), a failed send aborts the whole restore,
and the post-commit sync failure is reported as what it is — a committed
restore with a stale cache — never as a rollback that cannot happen.

Applying is one strictly scoped transaction (:func:`apply_backup`): the texts
and the settings are two sections of a single change, and any failure rolls the
whole restore back — a restore must never leave half a configuration behind.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import asyncpg

from core import database
from core.texts import validate_text

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BACKUP_BYTES",
    "PENDING_TTL_S",
    "SCHEMA_VERSION",
    "Backup",
    "BackupError",
    "apply_backup",
    "build_backup",
    "drop_pending",
    "is_backup_key",
    "remember_pending",
    "take_pending",
    "validate_backup",
]

#: The backup file's own format version — what a restore checks first. Bump it
#: when the file's shape changes; an older or newer file is refused, not guessed
#: at (a half-understood restore is worse than a refused one).
SCHEMA_VERSION = 1

#: How large an uploaded backup may be. A configuration file is small; anything
#: bigger is not one, and must not be read into memory to find that out.
MAX_BACKUP_BYTES = 1024 * 1024

#: Secret words, matched as whole *segments* of a key (segments are split on
#: ``_ : . -``): ``bot_token``, ``webhook_secret`` and ``session_id`` are
#: refused, while ``keyboard``, ``cache_key``, ``author`` and
#: ``youtube_session_server`` are deliberately *not*. A substring match would
#: destroy valid configuration on false positives — the classic ``_at`` and
#: ``key`` mistakes this rule is written against.
_SECRET_SEGMENTS = frozenset(
    {
        "token",
        "tokens",
        "secret",
        "secrets",
        "password",
        "passwords",
        "passwd",
        "passphrase",
        "passphrases",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "otp",
        "authorization",
    }
)

#: Credential *compounds*: two ordinary words that together name a secret.
_SECRET_COMPOUNDS = (
    frozenset({"api", "key"}),
    frozenset({"private", "key"}),
    frozenset({"access", "key"}),
    frozenset({"session", "id"}),
    frozenset({"auth", "key"}),
)

#: Generated runtime state: timestamp stamps (``…_sent_at``), down-since and
#: last-use markers, health facts (``cobalt_status``) — rebuilt by the running
#: bot, never configuration. Anchored patterns, not substrings.
_RUNTIME_RE = re.compile(
    r"_(?:at|since)$|_(?:last_use|status)$|^(?:helper_down_since|helper_alerted_at):",
    re.IGNORECASE,
)

#: The only fields a backup file may carry. Anything else — a token someone
#: pasted next to the JSON, an export's extra column — is a prohibited field.
_TOP_LEVEL_KEYS = frozenset({"schema_version", "created_at", "bot_texts", "bot_state"})


class BackupError(ValueError):
    """Why a file cannot be trusted with the database.

    The reason is a stable token (``format`` / ``schema`` / ``prohibited`` / or
    the text validator's own ``locked`` / ``length`` / ``placeholder`` /
    ``markup``) so the chat can render it in the reader's language instead of
    shipping this module's English — the same contract ``core.texts.validate_text``
    keeps.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _segments(key: str) -> set[str]:
    return {part for part in re.split(r"[_:.\-\s]+", key.lower()) if part}


def _is_secret_key(key: str) -> bool:
    """Whether a *field name* names a credential — whole words, never substrings."""
    segments = _segments(key)
    if segments & _SECRET_SEGMENTS:
        return True
    return any(compound <= segments for compound in _SECRET_COMPOUNDS)


def _find_secret_field(value: Any) -> str | None:
    """A credential-named field anywhere in a structure — however deeply nested.

    Only field *names* are judged: values are user content (a help text may
    legitimately say "token") and must never decide a restore. Returns the
    offending name, or ``None``.
    """
    if isinstance(value, dict):
        for key, nested in value.items():
            if isinstance(key, str) and _is_secret_key(key):
                return key
            found = _find_secret_field(nested)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_secret_field(item)
            if found is not None:
                return found
    return None


def is_backup_key(key: str) -> bool:
    """Whether a ``bot_state`` key is *settings* — and may travel in a backup.

    One rule for both doors: :func:`build_backup` drops the keys this refuses
    (tokens, credentials, runtime caches), and :func:`validate_backup` refuses a
    file that carries one anyway.
    """
    return not _is_secret_key(key) and _RUNTIME_RE.search(key) is None


@dataclass(frozen=True)
class Backup:
    """A configuration snapshot: the texts and the settings, nothing else."""

    #: ``(key, lang, value)`` — every custom text.
    texts: tuple[tuple[str, str, str], ...] = ()
    #: ``(key, value)`` — the settings rows a backup may carry.
    state: tuple[tuple[str, str], ...] = ()
    #: When the snapshot was taken (ISO 8601); empty for a file that never said.
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The file shape — exactly what :func:`validate_backup` reads back."""
        return {
            "schema_version": SCHEMA_VERSION,
            "created_at": self.created_at,
            "bot_texts": [
                {"key": key, "lang": lang, "value": value}
                for key, lang, value in self.texts
            ],
            "bot_state": dict(self.state),
        }


async def build_backup(pool: asyncpg.Pool) -> Backup:
    """Snapshot the configuration: every custom text, every settings row.

    Two exclusions run before the file exists, both deliberate: any key
    :func:`is_backup_key` refuses (tokens, credentials, generated runtime
    state), and any text that no longer validates against the catalogue (a key
    a deploy retired could never be applied again anyway). What the backup
    writes is what the restore can read back — by construction.
    """
    text_rows = await database.text_overrides(pool)
    state_rows = await database.all_state(pool)
    texts = tuple(
        sorted(
            (str(row["key"]), str(row["lang"]), str(row["value"]))
            for row in text_rows
            if validate_text(str(row["key"]), str(row["value"])) is None
        )
    )
    state = tuple(
        sorted(
            (str(row["key"]), str(row["value"]))
            for row in state_rows
            if is_backup_key(str(row["key"]))
        )
    )
    return Backup(
        texts=texts,
        state=state,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def validate_backup(data: object) -> Backup:
    """Judge an uploaded file: schema version, format, prohibited fields.

    Raises :class:`BackupError` (with the reason token) and changes nothing.
    The gate is deliberately at least as strict as the builder's: only the four
    known fields, only rows shaped exactly like rows, only settings keys that
    :func:`is_backup_key` allows — and every text passes the same validator an
    editor's save passes.
    """
    if not isinstance(data, dict):
        raise BackupError("format")
    if _find_secret_field(data) is not None:
        # A credential-named field anywhere in the tree — however deeply
        # nested — refuses the whole file.
        raise BackupError("prohibited")
    if set(data) - _TOP_LEVEL_KEYS:
        # A field this backup format does not have is either somebody's extra
        # column or somebody's credential — either way it does not come in.
        raise BackupError("prohibited")
    if not {"schema_version", "bot_texts", "bot_state"}.issubset(data):
        raise BackupError("format")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise BackupError("schema")

    raw_texts = data.get("bot_texts")
    if not isinstance(raw_texts, list):
        raise BackupError("format")
    texts: list[tuple[str, str, str]] = []
    for row in raw_texts:
        if not isinstance(row, dict):
            raise BackupError("format")
        if set(row) != {"key", "lang", "value"}:
            # A row carrying a field this format does not have is as prohibited
            # as a top-level one; a row missing one is simply malformed.
            raise BackupError("prohibited" if set(row) - {"key", "lang", "value"} else "format")
        key, lang, value = row["key"], row["lang"], row["value"]
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(lang, str)
            or not lang
            or not isinstance(value, str)
        ):
            raise BackupError("format")
        problem = validate_text(key, value)
        if problem:
            raise BackupError(problem)
        texts.append((key, lang, value))

    raw_state = data.get("bot_state")
    if not isinstance(raw_state, dict):
        raise BackupError("format")
    state: list[tuple[str, str]] = []
    for key, value in raw_state.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise BackupError("format")
        if not is_backup_key(key):
            raise BackupError("prohibited")
        state.append((key, value))

    created = data.get("created_at", "")
    if not isinstance(created, str):
        raise BackupError("format")
    return Backup(
        texts=tuple(sorted(texts)),
        state=tuple(sorted(state)),
        created_at=created,
    )


#: How long a confirmation lives. Short on purpose: the preview is on the
#: owner's screen *now* — a pending restore is a loaded button, not a bookmark.
PENDING_TTL_S = 5 * 60

#: Cap on remembered restorations (oldest dropped first): a convenience store
#: behind one button tap, not a queue.
_PENDING_CAP = 100

#: Confirmation nonce → ``(expires at, owner, the validated restore)``. The
#: payload lives *here*, server-side — never in callback data — bound to the
#: owner and to a strict expiration. Process-local like the retry store
#: (``core.ui``): one gateway, one memory.
_pending: dict[str, tuple[float, int, Backup]] = {}


def remember_pending(
    backup: Backup, *, owner: int, now: float | None = None
) -> str:
    """Keep a validated restore behind a fresh nonce — all the confirmation
    button carries. Bound to ``owner`` and to :data:`PENDING_TTL_S`."""
    moment = time.monotonic() if now is None else now
    for key, (expires, _, _) in list(_pending.items()):
        if expires <= moment:
            _pending.pop(key, None)
    while len(_pending) >= _PENDING_CAP:
        _pending.pop(next(iter(_pending)))
    nonce = secrets.token_urlsafe(9)
    _pending[nonce] = (moment + PENDING_TTL_S, owner, backup)
    return nonce


def take_pending(
    nonce: str, *, owner: int | None = None, now: float | None = None
) -> Backup | None:
    """Atomically consume a pending restore — **exactly one** confirmation wins.

    The pop is synchronous, so two callbacks racing in the same millisecond are
    serialised by it: the loser finds nothing and nothing runs twice. A
    stranger's tap finds nothing *and leaves the entry alone* — a crafted
    callback must not spend (or destroy) the owner's pending restore — and an
    expired entry is gone on sight.
    """
    moment = time.monotonic() if now is None else now
    entry = _pending.get(nonce)
    if entry is None:
        return None
    expires, holder, backup = entry
    if expires <= moment:
        _pending.pop(nonce, None)
        return None
    if owner is not None and holder != owner:
        return None
    _pending.pop(nonce, None)
    return backup


def drop_pending(nonce: str, *, owner: int | None = None) -> None:
    """Discard a pending restore (the owner cancelled) — owner-bound like the
    rest: somebody else's tap can neither spend it nor clear it."""
    entry = _pending.get(nonce)
    if entry is None:
        return
    _, holder, _ = entry
    if owner is not None and holder != owner:
        return
    _pending.pop(nonce, None)


async def apply_backup(pool: asyncpg.Pool, backup: Backup) -> None:
    """Apply a validated restore inside one strictly scoped transaction.

    The texts and the settings are two sections of one change: if any statement
    fails, the transaction rolls back and the database is exactly as it was.
    Settings rows the backup does not name are dropped (the configuration goes
    back to the snapshot), while runtime rows are left alone — they are not
    configuration and a restore has no business rewriting them.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await database.replace_text_overrides(conn, backup.texts)
            keep = {key for key, _ in backup.state}
            drop = [
                str(row["key"])
                for row in await database.all_state(conn)
                if str(row["key"]) not in keep and is_backup_key(str(row["key"]))
            ]
            await database.replace_state_rows(conn, backup.state, drop)
    logger.info(
        "restore applied: %d text(s), %d setting(s), %d dropped",
        len(backup.texts),
        len(backup.state),
        len(drop),
    )
