"""Admin-editable user-facing texts: a validated override layer over the catalogue.

:mod:`core.catalog` stays the source of truth — every key's bilingual default
lives there, and an edit is a *replacement for one key in one language*, never a
new message invented outside the catalogue. Three pieces live here, all of them
deliberate:

* **what may be edited** (:data:`CATEGORIES`) — user-facing families, grouped by
  feature so the admin screens are a menu of features rather than a wall of
  keys. Operator screens (``admin.``/``panel.``) and the Telegram command menu
  (``cmd.`` — changing it would need a re-publish through ``setMyCommands``, so
  an edit alone would silently *not* apply) are deliberately locked;
* **what a replacement must look like** (:func:`validate_text`) — bounded
  length, only placeholders the default already has (an override can never
  smuggle a new ``{field}`` — or a ``{x.__class__}`` attribute trick — into a
  ``str.format`` call site), and balanced Telegram-HTML markup with no
  unsupported tags. Invalid markup must never break a send, so it is refused at
  the desk instead of at the chat;
* **the runtime lookup** (:func:`override_for`) — an in-memory snapshot of
  ``bot_texts`` that :mod:`core.i18n` consults before the catalogue. ``t()``
  keeps its synchronous signature (it is called from everywhere, including the
  workers' narration) and therefore reads memory only — keeping the snapshot
  *true* is :class:`OverrideStore`'s job: a shared version counter (Redis) tells
  every process when to reload, a bounded TTL covers a missing counter, and the
  database stays the source of truth throughout. A save or reset is live in
  every process without a restart.

Safe defaults are the whole migration story: no row in ``bot_texts`` means the
catalogue default, and a reset is a delete.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from string import Formatter
from typing import Any, Iterable, Optional

from core.catalog import MESSAGES

logger = logging.getLogger(__name__)

#: A Telegram message is 4096 characters; leave the same margin the panel uses
#: so an edited text can never be sent whole and refused for length.
MAX_TEXT_LENGTH = 4000

#: Feature → catalogue key prefixes, the order the admin screens list them.
#: Every user-facing family is editable; the locked families below are not.
_CATEGORY_PREFIXES: dict[str, tuple[str, ...]] = {
    "start": ("start.", "menu.", "misc."),
    "language": ("language.",),
    "intake": ("intake.", "preflight."),
    "media": ("media.", "fmt.", "audio."),
    "download": ("work.", "download."),
    "errors": ("err.",),
    "profile": ("profile.", "premium.", "support."),
    "pay": ("pay.", "plan."),
}

#: Category id → its label in the catalogue (bilingual like everything else).
CATEGORY_LABEL_KEYS: dict[str, str] = {
    "start": "admin.textcat_start",
    "language": "admin.textcat_language",
    "intake": "admin.textcat_intake",
    "media": "admin.textcat_media",
    "download": "admin.textcat_download",
    "errors": "admin.textcat_errors",
    "profile": "admin.textcat_profile",
    "pay": "admin.textcat_pay",
}

#: Never editable — see the module docstring for why each one is locked.
_LOCKED_PREFIXES: tuple[str, ...] = ("admin.", "panel.", "cmd.")

#: The markup Telegram's HTML parse mode understands. Anything else (``<br>``,
#: ``<font>``, a stray ``<b>`` left unclosed) is refused at save time rather
#: than failing every send that uses the text.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "tg-spoiler",
        "a",
        "code",
        "pre",
        "blockquote",
    }
)

#: Link schemes a ``<a href>`` may point at.
_LINK_SCHEMES: tuple[str, ...] = ("http://", "https://", "tg://")

_formatter = Formatter()

#: The live snapshot lives in :class:`OverrideStore` (bottom of this file): one
#: per process, coherent across processes. Nothing here reads it directly — the
#: module-level accessors below delegate to the shared instance.


def category_ids() -> tuple[str, ...]:
    """The category ids, in menu order."""
    return tuple(_CATEGORY_PREFIXES)


def category_keys(category: str) -> tuple[str, ...]:
    """Every editable key of one category, sorted — ``()`` for no such category."""
    prefixes = _CATEGORY_PREFIXES.get(category)
    if prefixes is None:
        return ()
    return tuple(sorted(key for key in MESSAGES if key.startswith(prefixes)))


def editable(key: str) -> bool:
    """Whether this key may be edited at all (user-facing, not locked)."""
    return bool(key) and not key.startswith(_LOCKED_PREFIXES)


def category_of(key: str) -> str | None:
    """The category a key lists under — ``None`` when it is editable nowhere."""
    for category, prefixes in _CATEGORY_PREFIXES.items():
        if key.startswith(prefixes):
            return category
    return None


def _placeholders(template: str) -> Optional[set[str]]:
    """The ``{field}`` names a template formats with — ``None`` when malformed.

    Field *names* only: ``{name!r:>10}`` is the field ``name``, and a template
    that does not parse at all (a lone ``{``) answers ``None`` rather than a
    partial set.
    """
    try:
        return {
            field
            for _literal, field, _spec, _conv in _formatter.parse(template)
            if field
        }
    except ValueError:
        return None


class _MarkupCheck(HTMLParser):
    """Balanced, Telegram-supported tags and sane links — or ``problem`` is set."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problem = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if self.problem:
            return
        if tag not in _ALLOWED_TAGS:
            self.problem = True
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not href.startswith(_LINK_SCHEMES):
                self.problem = True
                return
        self.stack.append(tag)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        # Telegram's HTML has no self-closing tags (<br/> and friends are refused
        # by the API itself — refusing here keeps the failure at the desk).
        self.problem = True

    def handle_endtag(self, tag: str) -> None:
        if self.problem:
            return
        if not self.stack or self.stack.pop() != tag:
            self.problem = True

    def finish(self) -> bool:
        """``True`` when the markup is sound (checks the unclosed tags too)."""
        try:
            self.close()
        except Exception:  # noqa: BLE001 — a parser quirk is a "no" like any other
            return False
        return not self.problem and not self.stack


def _markup_ok(value: str) -> bool:
    check = _MarkupCheck()
    try:
        check.feed(value)
    except Exception:  # noqa: BLE001 — anything unparseable is unsendable markup
        return False
    return check.finish()


def validate_text(key: str, value: str) -> Optional[str]:
    """Whether an edit may be saved — ``None`` yes, else the reason it cannot.

    The reason is a stable token (``locked`` / ``length`` / ``placeholder`` /
    ``markup``) so the caller renders it in the reader's language instead of
    shipping this module's English to the chat. Deliberately the *only* gate a
    replacement passes: every check here exists so an edit can never break a
    send (unknown placeholders, unbalanced tags, over-long text) or widen what a
    template may reach (new fields, attribute/index tricks in a field name).
    """
    if not editable(key) or key not in MESSAGES:
        return "locked"
    if len(value) > MAX_TEXT_LENGTH:
        return "length"
    fields = _placeholders(value)
    if fields is None:
        return "placeholder"
    default_fields = _placeholders(MESSAGES[key]["en"]) or set()
    if not fields <= default_fields:
        return "placeholder"
    if not _markup_ok(value):
        return "markup"
    return None


def override_for(key: str, lang: str) -> Optional[str]:
    """The stored replacement for ``(key, lang)``, or ``None`` (use the default)."""
    return _store.override_for(key, lang)


def effective(key: str, lang: str) -> str:
    """What this text *is* right now — the replacement, else the catalogue default.

    What the editor shows and previews: never a stale copy. The catalogue
    guarantees both languages exist, and a language whose key went missing falls
    back to English like everything else.
    """
    return _store.effective(key, lang)


def is_edited(key: str, lang: str) -> bool:
    """Whether an admin has replaced this text (the screens mark it so)."""
    return _store.is_edited(key, lang)


def set_override(key: str, lang: str, value: str) -> None:
    """Take a validated replacement live in *this* process (no publish — the
    writer that must reach other processes uses :func:`saved`)."""
    _store.set_override(key, lang, value)


def clear_override(key: str, lang: str) -> None:
    """Drop a replacement locally (no publish — the writer that must reach
    other processes uses :func:`cleared`)."""
    _store.clear_override(key, lang)


def apply_overrides(rows: Iterable[Any]) -> int:
    """Load database rows (``key``, ``lang``, ``value``, …) into the map.

    Returns how many replacements are now live. Rows naming keys that no longer
    exist in the catalogue are skipped (a key removed by a deploy must not make
    ``t`` raise), and so are rows that no longer validate — stale rows can never
    outlive their check.
    """
    return _store.apply_overrides(rows)


async def saved(key: str, lang: str, value: str) -> None:
    """A replacement reached the database: take it live here, tell the peers."""
    await _store.saved(key, lang, value)


async def cleared(key: str, lang: str) -> None:
    """A replacement was reset: drop it here, tell the peers."""
    await _store.cleared(key, lang)


def bind_store(
    loader: Callable[[], Awaitable[Iterable[Any]]], channel: Any | None = None
) -> None:
    """Wire the shared store to the database (source of truth) and a channel."""
    _store.bind(loader, channel)


async def sync_overrides(*, force: bool = False) -> bool:
    """Refresh the shared snapshot when it may be stale (see
    :meth:`OverrideStore.sync`)."""
    return await _store.sync(force=force)


#: The Redis key holding the change counter (see :class:`RedisVersionChannel`).
VERSION_KEY = "bot:texts:version"

#: How stale one process's snapshot may get with no working version channel
#: before the database is re-read — the staleness *bound* for the Redis-down
#: deployment.
FALLBACK_TTL_S = 30.0

#: One coherence check per this interval per process: ``sync()`` runs on every
#: update and every job, and the texts change a few times a month.
SYNC_MIN_INTERVAL_S = 5.0


def _as_text(value: Any) -> str:
    """Redis answers bytes or text depending on the client's settings."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


class RedisVersionChannel:
    """The invalidation signal between processes: one Redis counter.

    A save/reset bumps ``bot:texts:version`` through :meth:`bump`; every
    process's periodic
    :meth:`OverrideStore.sync` compares the counter and reloads ``bot_texts``
    only when it moved — one cheap ``GET``, never a query per message and no
    pub/sub connection to keep alive. Redis being *down* is a normal state, not
    an error: the accessor raises, the store treats the channel as absent and
    the TTL fallback takes over (see :class:`OverrideStore`).
    """

    def __init__(self, redis: Any, key: str = VERSION_KEY) -> None:
        self._redis = redis
        self._key = key

    async def version(self) -> str:
        """The current counter — ``"0"`` before the first change."""
        return _as_text(await self._redis.get(self._key)) or "0"

    async def bump(self) -> str:
        """Register one change and answer the new counter."""
        return _as_text(await self._redis.incr(self._key))


class OverrideStore:
    """One process's live view of ``bot_texts`` — cheap to read, coherent enough.

    ``t()`` is synchronous and hot, so every accessor reads the in-memory
    snapshot and nothing else: **no database query ever happens inside a text
    lookup**. Keeping that snapshot true across processes is :meth:`sync`'s job,
    driven from where async already is (the update middleware, the worker loop)
    and bounded two ways:

    * **invalidation** — a version channel (:class:`RedisVersionChannel`) shared
      by every process: a save bumps it, a sync reloads the table only when the
      counter moved. No restart is ever needed for an edit to go live everywhere.
    * **a bounded TTL** — with no reachable channel (Redis down; a memory-queue
      deployment), the database — the source of truth — is re-read at most every
      :data:`FALLBACK_TTL_S` seconds, so staleness stays bounded regardless.

    Failure is a state here, never an exception on a user's path: a reload that
    fails keeps the last known snapshot (an old text beats a broken chat), and a
    channel that fails is simply absent. :meth:`saved`/:meth:`cleared` apply
    locally first and publish second, so the process that made the change sees
    it immediately.

    Concurrency, stated once: edits are **last-write-wins** over the database
    upsert, and every write leaves its own audit row with the before/after
    values — an overwritten value is never lost from the history, only from the
    present.
    """

    def __init__(
        self,
        *,
        ttl_s: float = FALLBACK_TTL_S,
        min_interval_s: float = SYNC_MIN_INTERVAL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._overrides: dict[tuple[str, str], str] = {}
        self._loader: Callable[[], Awaitable[Iterable[Any]]] | None = None
        self._channel: Any | None = None
        self._version: str | None = None
        self._ttl_s = ttl_s
        self._min_interval_s = min_interval_s
        self._clock = clock or time.monotonic
        self._checked_at = float("-inf")
        self._loaded_at = float("-inf")

    # -- wiring (once, at boot) ------------------------------------------------

    def bind(
        self,
        loader: Callable[[], Awaitable[Iterable[Any]]],
        channel: Any | None = None,
    ) -> None:
        """Attach the database loader and the optional invalidation channel."""
        self._loader = loader
        self._channel = channel

    # -- synchronous reads: what t() and the screens use -----------------------

    def override_for(self, key: str, lang: str) -> Optional[str]:
        return self._overrides.get((key, lang))

    def effective(self, key: str, lang: str) -> str:
        entry = MESSAGES[key]
        return self._overrides.get((key, lang)) or entry.get(lang) or entry["en"]

    def is_edited(self, key: str, lang: str) -> bool:
        return (key, lang) in self._overrides

    # -- local mutation ---------------------------------------------------------

    def set_override(self, key: str, lang: str, value: str) -> None:
        self._overrides[(key, lang)] = value

    def clear_override(self, key: str, lang: str) -> None:
        self._overrides.pop((key, lang), None)

    def apply_overrides(self, rows: Iterable[Any]) -> int:
        """Load database rows (``key``, ``lang``, ``value``, …) into the map.

        Returns how many replacements are now live. Rows naming keys that no
        longer exist in the catalogue are skipped (a key removed by a deploy
        must not make ``t`` raise), and so are rows that no longer validate —
        stale rows can never outlive their check.
        """
        self._overrides.clear()
        loaded = 0
        for row in rows:
            key = str(row["key"])
            lang = str(row["lang"])
            value = str(row["value"])
            if key not in MESSAGES:
                logger.warning("text override for unknown key %r ignored", key)
                continue
            problem = validate_text(key, value)
            if problem:
                logger.warning(
                    "text override for %s (%s) no longer validates (%s) — ignored",
                    key,
                    lang,
                    problem,
                )
                continue
            self._overrides[(key, lang)] = value
            loaded += 1
        self._loaded_at = self._clock()
        return loaded

    # -- async coherence ---------------------------------------------------------

    async def sync(self, *, force: bool = False) -> bool:
        """Refresh the snapshot when it may be stale. ``True`` when it reloaded.

        Rate-limited to one coherence check per :data:`SYNC_MIN_INTERVAL_S`
        unless ``force``. The check itself is one channel read (or, without a
        channel, one clock read); the database is touched only when the counter
        moved or the TTL expired. Never raises — coherence trouble is logged and
        the last known snapshot stays live.
        """
        if self._loader is None:
            return False
        now = self._clock()
        if not force and now - self._checked_at < self._min_interval_s:
            return False
        self._checked_at = now
        version = await self._channel_version()
        if version is not None:
            if version == self._version:
                return False
        elif now - self._loaded_at < self._ttl_s:
            # No working channel: the database is re-read on the TTL, and that
            # bound is the whole answer to "when does another process see this".
            return False
        rows = await self._reload()
        if rows is None:
            return False
        self.apply_overrides(rows)
        self._version = version
        return True

    async def saved(self, key: str, lang: str, value: str) -> None:
        """A replacement reached the database: take it live here, tell the peers."""
        self.set_override(key, lang, value)
        await self._publish()

    async def cleared(self, key: str, lang: str) -> None:
        """A replacement was reset: drop it here, tell the peers."""
        self.clear_override(key, lang)
        await self._publish()

    # -- internals ---------------------------------------------------------------

    async def _channel_version(self) -> Optional[str]:
        if self._channel is None:
            return None
        try:
            return str(await self._channel.version())
        except Exception:  # noqa: BLE001 — a dead channel is a degraded mode, not an error
            logger.debug("text-override version channel unavailable", exc_info=True)
            return None

    async def _reload(self) -> Optional[list[Any]]:
        if self._loader is None:  # only bind() gets us here; belt and braces
            return None
        try:
            return list(await self._loader())
        except Exception:  # noqa: BLE001 — the last known snapshot stays live
            logger.warning(
                "could not reload text overrides — keeping the last known set",
                exc_info=True,
            )
            return None

    async def _publish(self) -> None:
        self._loaded_at = self._clock()
        if self._channel is None:
            return
        try:
            self._version = str(await self._channel.bump())
        except Exception:  # noqa: BLE001 — peers fall back to the TTL bound
            logger.warning(
                "could not publish a text change — other processes will pick it "
                "up within the TTL",
                exc_info=True,
            )
            self._version = None


#: The process-wide store that :mod:`core.i18n` and the admin editor share.
#: The cross-process contract is pinned by two independent instances meeting
#: over one fake channel/database in ``tests/test_text_sync.py``.
_store = OverrideStore()
