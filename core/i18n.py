"""Bilingual messages: one catalogue, two languages, resolved where the user is.

Every user-facing string in this bot is looked up by *key* instead of being written
where it is sent. Three things fall out of that, and all three are the reason for
the indirection:

* a user's language is a property of the user (``users.language``), not of the
  call site — the same progress message has to arrive in Persian for one person and
  English for the next, including from a background worker that has never seen the
  update;
* translations sit next to each other in :mod:`core.catalog`, so a missing one is
  visible in a diff instead of hiding in a second file;
* a test can assert the shape of the catalogue (every key has both languages, the
  placeholders match) rather than grepping for strings.

Nothing here guesses: an unknown language is English, and a Persian Telegram locale
(``fa``, ``fa-IR``) is Persian, so a Persian user sees Persian before touching
anything. ``/language`` and the two buttons on the welcome screen change it for good.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import Literal

from core.catalog import MESSAGES, SEEDED_PLAN_ALIASES

logger = logging.getLogger(__name__)

#: The languages this bot speaks. Adding a third is a catalogue, a name, a flag —
#: and this line.
Lang = Literal["en", "fa"]
LANGS: tuple[Lang, ...] = ("en", "fa")

#: What a new user gets unless their Telegram locale says otherwise.
DEFAULT_LANG: Lang = "en"

#: How each language calls itself (never translate these).
_LANGUAGE_NAMES: dict[Lang, str] = {"en": "English", "fa": "فارسی"}
_LANGUAGE_FLAGS: dict[Lang, str] = {"en": "🇬🇧", "fa": "🇮🇷"}

#: Spellings a person might store/tap that are not the bare code.
_ALIASES: dict[str, Lang] = {
    "persian": "fa",
    "farsi": "fa",
    "parsi": "fa",
    "fa-ir": "fa",
    "en-us": "en",
    "en-gb": "en",
    "english": "en",
}

_LOCALE_SPLIT = re.compile(r"[-_]")

__all__ = [
    "DEFAULT_LANG",
    "LANGS",
    "Lang",
    "detect_lang",
    "error_message",
    "lang_button",
    "lang_of",
    "language_flag",
    "language_name",
    "language_options",
    "normalize_lang",
    "normalize_supported",
    "t",
]


def normalize_lang(value: object, default: str | None = None) -> Lang:
    """Coerce *anything* (a locale, a DB value, a button tap) to a supported language.

    ``fa``, ``fa-IR``, ``fa_IR``, ``persian`` and ``FARSI`` are Persian; every other
    value — including an empty one — is ``default``. This doubles as the detector for
    Telegram's ``language_code``: the same normalization has to happen for a stored
    preference and a fresh update, and ``default`` is what lets an operator deploy a
    bot whose *default* is Persian (``DEFAULT_LANGUAGE``) without touching the code.
    """
    fallback = normalize_supported(default) or DEFAULT_LANG
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    primary = _LOCALE_SPLIT.split(text)[0]
    for lang in LANGS:
        if primary == lang:
            return lang
    return _ALIASES.get(text) or _ALIASES.get(primary) or fallback


def normalize_supported(value: object) -> Lang | None:
    """``value`` when it *is* a supported language, else ``None`` (no defaulting).

    The difference from :func:`normalize_lang` is the point: a typo has to be
    *rejected* where a preference is being set, and *forgiven* where one is being
    read. Both spellings of the same language are accepted here too (``fa-IR``,
    ``persian``), because refusing those would be pedantry rather than safety.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    primary = _LOCALE_SPLIT.split(text)[0]
    for lang in LANGS:
        if primary == lang:
            return lang
    return _ALIASES.get(text) or _ALIASES.get(primary)


#: Readable alias: what the middleware does to ``from_user.language_code``.
detect_lang = normalize_lang


def lang_of(user: object, default: str | None = None) -> Lang:
    """The language stored for a user record, or ``default``/English.

    Tolerates a plain mapping and a record without the column (an older row, a test
    stub): a missing language must never be an exception on the path that is supposed
    to *say something*.
    """
    with contextlib.suppress(KeyError, IndexError, TypeError):
        return normalize_lang(user["language"], default)  # type: ignore[index]
    return normalize_lang("", default)


def t(key: str, lang: object = None, /, **values: object) -> str:
    """The message for ``key`` in ``lang``, formatted with ``values``.

    A key that does not exist is a programming error and raises: a typo that silently
    returns the key itself is how users end up reading ``work.uploading`` in a chat.
    A *language* without that key, on the other hand, falls back to the default one —
    a half-translated catalogue must still produce a sentence.
    """
    entry = MESSAGES.get(key)
    if entry is None:
        raise KeyError(f"unknown message key: {key!r}")
    template = entry.get(normalize_lang(lang)) or entry[DEFAULT_LANG]
    return template.format(**values) if values else template


def error_message(code: str, lang: object = None, fallback: str = "") -> str:
    """The user-facing text for an extractor error code.

    Looser than :func:`t` on purpose: error codes come from the engine and can be
    added without a translation, and a download that failed for an untranslated
    reason still has to tell the user something. The engine's own message (Persian,
    and written where the failure was diagnosed) is that something.
    """
    entry = MESSAGES.get(f"err.{code}")
    if entry is None:
        return fallback or MESSAGES["err.GENERAL"].get(
            normalize_lang(lang), MESSAGES["err.GENERAL"][DEFAULT_LANG]
        )
    template = entry.get(normalize_lang(lang)) or entry[DEFAULT_LANG]
    return template.format(detail=fallback) if "{detail}" in template else template


def plan_name(name: object, lang: object = None) -> str:
    """A plan's label in ``lang``, for the plans the installer itself wrote.

    The database row is the source of truth — an operator can rename a plan, and a
    translation that overrode their name would be a bug they could not see. So only
    the seeded labels are looked up (:data:`core.catalog.SEEDED_PLAN_ALIASES`) and
    anything else is returned exactly as stored.
    """
    label = str(name or "").strip()
    key = SEEDED_PLAN_ALIASES.get(label)
    return t(key, lang) if key else label


def language_name(lang: object) -> str:
    """How a language names itself (``English`` / ``فارسی``)."""
    return _LANGUAGE_NAMES[normalize_lang(lang)]


def language_flag(lang: object) -> str:
    """The flag that goes with it in a picker."""
    return _LANGUAGE_FLAGS[normalize_lang(lang)]


def lang_button(lang: object) -> str:
    """The picker label for a language: flag plus its own name."""
    return f"{language_flag(lang)} {language_name(lang)}"


def language_options() -> list[tuple[Lang, str]]:
    """``[(code, label)]`` for a language keyboard, in catalogue order."""
    return [(lang, lang_button(lang)) for lang in LANGS]
