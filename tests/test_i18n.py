"""The bilingual layer: the catalogue's invariants and the language rules.

A catalogue is the kind of file where mistakes are invisible until a user sees them
(a key that exists in one language only, a placeholder renamed in one of the two, a
typo'd ``{cousin}`` where ``{count}`` was meant). All three are cheap to check and
expensive to notice, so they are checked here rather than reviewed.

The second half pins the *resolution* rules: a Telegram locale decides what a new
user starts with, a stored choice always wins, and a language the bot does not speak
is refused where it is being set — but forgiven where it is being read.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.catalog import MESSAGES
from core.i18n import (
    DEFAULT_LANG,
    LANGS,
    error_message,
    lang_button,
    lang_of,
    language_flag,
    language_name,
    language_options,
    normalize_lang,
    normalize_supported,
    plan_name,
    t,
)

PLACEHOLDER = re.compile(r"\{(\w+)")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


def test_every_key_exists_in_every_language() -> None:
    missing = {
        key: [lang for lang in LANGS if not entry.get(lang)]
        for key, entry in MESSAGES.items()
        if any(not entry.get(lang) for lang in LANGS)
    }

    assert missing == {}, "a key with one language is a user reading a raw key"


def test_no_language_has_a_key_nobody_else_has() -> None:
    extra = {
        key: [lang for lang in entry if lang not in LANGS]
        for key, entry in MESSAGES.items()
        if any(lang not in LANGS for lang in entry)
    }

    assert extra == {}, "a stray language code is a translation nobody will ever see"


def test_placeholders_are_identical_in_both_languages() -> None:
    """A renamed placeholder raises ``KeyError`` at ``.format()`` — in a chat."""
    mismatched: dict[str, tuple[list[str], list[str]]] = {}
    for key, entry in MESSAGES.items():
        english = sorted(set(PLACEHOLDER.findall(entry["en"])))
        persian = sorted(set(PLACEHOLDER.findall(entry["fa"])))
        if english != persian:
            mismatched[key] = (english, persian)

    assert mismatched == {}, mismatched


def test_no_message_is_whitespace_or_empty() -> None:
    blank = [key for key, entry in MESSAGES.items() if not entry["en"].strip() or not entry["fa"].strip()]

    assert blank == []


def test_the_error_codes_the_engine_can_raise_are_translated() -> None:
    """Every failure a user can *read* has a sentence in their language.

    ``error_message`` falls back to the engine's own wording for a code it does not
    know, which is the right behaviour and also the reason this list exists: without
    it, a new code silently ships Persian text to an English user.
    """
    from services.extractor import BLOCK_EXTRACTION_CODES, DRM_PROTECTED_CODE, IMAGE_ONLY

    codes = {
        *BLOCK_EXTRACTION_CODES,
        DRM_PROTECTED_CODE,
        IMAGE_ONLY,
        "GENERAL",
        "UNSUPPORTED_URL",
        "PRIVATE_VIDEO",
        "AGE_RESTRICTED",
        "GEO_RESTRICTED",
        "LIVE_STREAM",
        "PLAYLIST_NOT_SUPPORTED",
        "FFMPEG_REQUIRED",
        "TIMEOUT",
        "SPOTIFY_NOT_A_TRACK",
        "SPOTIFY_LOOKUP_FAILED",
        "SPOTIFY_NO_MATCH",
    }
    # Codes raised directly (not through the error-mapping table) are found in the
    # source, so a `raise ExtractionError("NEW_CODE", …)` cannot slip through as a
    # missing translation the way a hand-maintained list would let it.
    source = (PROJECT_ROOT / "services" / "extractor.py").read_text(encoding="utf-8")
    codes |= set(re.findall(r'ExtractionError\(\s*"([A-Z][A-Z_]+)"', source))
    spotify_source = (PROJECT_ROOT / "services" / "spotify.py").read_text(encoding="utf-8")
    codes |= set(re.findall(r'ExtractionError\(\s*"([A-Z][A-Z_]+)"', spotify_source))

    untranslated = sorted(code for code in codes if f"err.{code}" not in MESSAGES)
    assert untranslated == [], untranslated


def test_a_seeded_plan_is_translated_where_it_is_shown() -> None:
    """The plan *row* is data, so only the labels the installer wrote are looked up.

    Every database seeded so far holds the Persian labels, and a user reading English
    must not be shown «۱ ماه» — while a plan an operator renamed must survive both
    languages exactly as typed.
    """
    assert plan_name("۱ ماه", "en") == "1 month"
    assert plan_name("۳ ماه", "fa") == "۳ ماه"
    assert plan_name("One year (VIP)", "fa") == "One year (VIP)"
    assert plan_name(None, "en") == "", "a missing row is empty, not a KeyError"


def test_every_seeded_plan_alias_points_at_a_translated_key() -> None:
    from core.catalog import SEEDED_PLAN_ALIASES

    missing = sorted(key for key in SEEDED_PLAN_ALIASES.values() if key not in MESSAGES)
    assert missing == [], missing


#: A sample value per placeholder name, so every message can be rendered as a user
#: would see it. ``percent`` is a float because its template carries a format spec
#: (``{percent:.0f}``), which is exactly the kind of thing that only breaks once
#: somebody is watching a download.
_SAMPLE_VALUES: dict[str, object] = {
    "percent": 42.0,
    "days": 3,
    "used": 2,
    "left": 8,
    "limit": 10,
    "depth": 1,
    "count": 5,
    "users": 120,
    "premium": 9,
    "new_users": 3,
    "downloads_today": 41,
    "active_today": 14,
    "cache_rows": 88,
    "blocks_24h": 5,
    "pending_txns": 2,
    "telegram_id": 1234,
    "resolution": "720p",
    "percent_done": "42%",
    "size": "2.0 MB",
    "done": "2.0 MB",
    "total": "5.0 MB",
    "duration": "1:35",
}


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("key", sorted(MESSAGES))
def test_every_message_renders_in_every_language(key: str, lang: str) -> None:
    """The strongest guarantee a catalogue can give offline: nothing a handler can
    ask for raises, leaves a placeholder behind, or needs a value nobody passes."""
    names = set(PLACEHOLDER.findall(MESSAGES[key][lang]))
    values = {name: _SAMPLE_VALUES.get(name, "x") for name in names}

    rendered = t(key, lang, **values)

    assert rendered.strip()
    assert "{" not in rendered and "}" not in rendered, rendered


def test_t_formats_and_escapes_nothing_it_was_not_asked_to() -> None:
    assert t("intake.queued", "en", depth=3) == "⏳ Added to the queue (position ≈ 3)."
    assert t("intake.queued", "fa", depth=3).endswith("(موقعیت تقریبی: 3).")


def test_t_refuses_a_key_nobody_defined() -> None:
    with pytest.raises(KeyError):
        t("menu.teleport")


def test_a_language_without_that_key_falls_back_to_the_default() -> None:
    assert t("menu.profile", "de") == t("menu.profile", DEFAULT_LANG)


# ---------------------------------------------------------------------------
# Errors by code
# ---------------------------------------------------------------------------


def test_an_unknown_error_code_keeps_the_engines_own_wording() -> None:
    """The engine's message is Persian and written where the failure happened; a
    download that failed for an untranslated reason must still say something."""
    assert error_message("SOMETHING_NEW", "en", fallback="engine said this") == "engine said this"


def test_an_unknown_error_code_without_a_fallback_is_generic_but_translated() -> None:
    assert error_message("SOMETHING_NEW", "en") == t("err.GENERAL", "en")


def test_a_known_error_code_is_translated_in_both_languages() -> None:
    assert "stale" in error_message("SESSION_STALE", "en")
    assert error_message("SESSION_STALE", "fa") != error_message("SESSION_STALE", "en")


# ---------------------------------------------------------------------------
# Resolving a language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("fa", "fa"),
        ("fa-IR", "fa"),
        ("fa_IR", "fa"),
        ("FA", "fa"),
        ("persian", "fa"),
        ("farsi", "fa"),
        ("en", "en"),
        ("en-US", "en"),
        ("English", "en"),
        ("", DEFAULT_LANG),
        (None, DEFAULT_LANG),
        ("de", DEFAULT_LANG),  # not ours → the default
    ),
)
def test_a_locale_is_normalized_to_a_language_we_speak(value: object, expected: str) -> None:
    assert normalize_lang(value) == expected


def test_the_default_language_is_configurable() -> None:
    """A Persian-facing deployment sets ``DEFAULT_LANGUAGE=fa`` and an unknown
    locale lands there instead of English."""
    assert normalize_lang("de", "fa") == "fa"
    assert normalize_lang("", "fa") == "fa"
    assert normalize_lang("en", "fa") == "en", "a locale we do speak still wins"


@pytest.mark.parametrize("value", ("", None, "de", "de-DE", "xx", "klingon", 7))
def test_setting_a_language_accepts_only_real_ones(value: object) -> None:
    assert normalize_supported(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    (("fa", "fa"), ("fa-IR", "fa"), ("EN", "en"), ("english", "en"), ("Persian", "fa")),
)
def test_setting_a_language_accepts_the_real_spellings(value: str, expected: str) -> None:
    """Including the names people actually type: ``/language english`` should work."""
    assert normalize_supported(value) == expected


def test_a_stored_preference_wins_over_a_missing_column() -> None:
    assert lang_of({"language": "fa"}, "en") == "fa"
    assert lang_of({"language": None}, "en") == "en"
    assert lang_of({}, "fa") == "fa", "an older row without the column reads as the default"
    assert lang_of(object(), "fa") == "fa", "and so does something that is not a row at all"


def test_every_language_names_itself_and_has_a_picker_label() -> None:
    assert language_name("fa") == "فارسی"
    assert language_name("en") == "English"
    assert language_flag("fa") == "🇮🇷"
    assert lang_button("en") == "🇬🇧 English"
    assert [code for code, _ in language_options()] == list(LANGS)
