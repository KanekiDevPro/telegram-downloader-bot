"""Lossless rows are hidden from menus, but stale taps still reach the refusal.

With no verified-lossless source the FLAC/WAV buttons are dead-end taps, so
no menu draws them — while the validation vocabulary keeps recognizing them,
and a tap from an old menu is answered with the existing refusal before
anything is queued. These tests pin both halves, offline.
"""

from __future__ import annotations

from typing import Any

import pytest

from core.i18n import error_message
from handlers import user as user_module
from services import content
from services.extractor import AudioCapability
from tests.test_user_menu import (
    EN,
    FA,
    FakeQueue,
    RecordingBot,
    _buttons,
    _callback,
    _state,
    _user,
)

SOUNDCLOUD = "https://soundcloud.com/a/b"
SPOTIFY_TRACK = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def _callbacks(markup: Any) -> list[str]:
    return [data for _label, data in _buttons(markup)]


def _full_capability() -> AudioCapability:
    """A probe that could build everything — lossless rows included."""
    return AudioCapability(
        formats=("mp3", "m4a", "flac", "opus", "wav"), copy_ok=True
    )


def test_the_predicate_is_false_today() -> None:
    assert content.lossless_offered() is False


def test_the_generic_grid_offers_no_lossless_rows() -> None:
    """Even a probe that could build FLAC/WAV draws no buttons for them."""
    keyboard = user_module._question_keyboard(
        SOUNDCLOUD, EN, capability=_full_capability()
    )
    callbacks = _callbacks(keyboard)

    assert "fmt:audio:flac" not in callbacks
    assert "fmt:audio:wav" not in callbacks
    assert "audf:mp3" in callbacks
    assert "audf:m4a" in callbacks, "the untouched stream is still offered"
    assert "audf:opus" in callbacks


def test_original_and_mp3_320_are_still_offered() -> None:
    levels = _callbacks(user_module._level_keyboard("mp3", EN))

    assert "fmt:audio:mp3.best" in levels, "MP3 320 keeps its row"
    originals = _callbacks(user_module._level_keyboard("m4a", EN))
    assert "fmt:audio:m4a" in originals, "the untouched stream keeps its row"


def test_the_spotify_menu_is_unchanged() -> None:
    """The track already drew two honest rows and never knew FLAC."""
    assert content.routing_for(SPOTIFY_TRACK).audio_formats == ("mp3",)

    keyboard = user_module._question_keyboard(
        SPOTIFY_TRACK, EN, capability=_full_capability()
    )
    callbacks = _callbacks(keyboard)

    assert "fmt:audio:mp3.best" in callbacks
    assert "fmt:audio:m4a" in callbacks
    assert "fmt:audio:flac" not in callbacks
    assert "fmt:audio:wav" not in callbacks


def test_monkeypatching_the_predicate_restores_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch works: a verified-lossless source brings the rows back."""
    monkeypatch.setattr(content, "lossless_offered", lambda: True)

    assert content.routing_for(SOUNDCLOUD).audio_formats == (
        "mp3",
        "m4a",
        "flac",
        "opus",
        "wav",
    )
    callbacks = _callbacks(
        user_module._question_keyboard(
            SOUNDCLOUD, EN, capability=_full_capability()
        )
    )

    assert "fmt:audio:flac" in callbacks
    assert "fmt:audio:wav" in callbacks


@pytest.mark.parametrize("quality", ("flac", "wav", "flac.best", "wav.best"))
@pytest.mark.parametrize(
    "data",
    (
        # A pre-deploy FSM still listing the rows as offered …
        {"audio_offered": ["mp3", "m4a", "flac", "opus", "wav"]},
        # … a new menu that never drew them …
        {"audio_offered": ["mp3", "m4a", "opus"]},
        # … and a state with no offered lists at all (the static fallback).
        {},
    ),
    ids=("stale-fsm", "new-menu", "no-vocabulary"),
)
def test_every_stale_lossless_tap_is_recognized(quality: str, data: dict[str, Any]) -> None:
    """Whatever state the tap lands in, it reads as stale — never as offered."""
    assert user_module._stale_lossless_tap(SOUNDCLOUD, "audio", quality, data) is True


@pytest.mark.parametrize(
    ("media_format", "quality", "data"),
    (
        ("audio", "mp3.best", {"audio_offered": ["mp3", "m4a", "opus"]}),
        ("audio", "m4a", {"audio_offered": ["mp3", "m4a", "opus"]}),
        ("audio", "opus.small", {"audio_offered": ["mp3", "m4a", "opus"]}),
        ("video", "720", {"offered": ["720"]}),
    ),
)
def test_honest_taps_are_never_mistaken_for_stale(
    media_format: str, quality: str, data: dict[str, Any]
) -> None:
    assert user_module._stale_lossless_tap(SOUNDCLOUD, media_format, quality, data) is False


def test_a_live_lossless_offering_is_not_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the rows exist, a tap the live menu offered proceeds as before."""
    monkeypatch.setattr(content, "lossless_offered", lambda: True)
    data = {"audio_offered": ["mp3", "m4a", "flac", "opus", "wav"]}

    assert user_module._stale_lossless_tap(SOUNDCLOUD, "audio", "flac", data) is False
    assert (
        user_module._tap_was_offered(SOUNDCLOUD, "audio", "flac", data) is True
    )


@pytest.mark.parametrize("lang", (EN, FA))
@pytest.mark.parametrize(
    "data",
    (
        {"audio_offered": ["mp3", "m4a", "flac", "opus", "wav"]},
        {"audio_offered": ["mp3", "m4a", "opus"]},
        {},
    ),
    ids=("stale-fsm", "new-menu", "no-vocabulary"),
)
async def test_a_stale_lossless_tap_gets_the_refusal_not_a_job(
    lang: str, data: dict[str, Any]
) -> None:
    """The existing refusal answers the tap; nothing is queued, no quota moves.

    ``pool`` is a bare object — any cache, quota or queue touch would explode —
    and the fake queue records that no download was enqueued.
    """
    bot = RecordingBot()
    queue = FakeQueue()
    state = await _state(SOUNDCLOUD)
    await state.update_data(**data)

    await user_module.on_format_chosen(
        _callback(bot, "fmt:audio:flac"),
        state,
        _user(),
        object(),
        queue,
        bot,
        lang=lang,
    )

    assert queue.tasks == [], "nothing is queued"
    assert bot.texts == [] and bot.edits == [], "no card, no message — just the answer"
    assert len(bot.answers) == 1
    assert bot.answers[0].show_alert is True
    assert bot.answers[0].text == error_message("LOSSLESS_UNAVAILABLE", lang)
    assert "stale" not in (bot.answers[0].text or "").lower()


def test_the_refusal_names_the_honest_alternatives() -> None:
    """LOSSLESS_UNAVAILABLE already points at Original and MP3 320, in en+fa —
    pinned so no future edit can silently drop the way out."""
    en = error_message("LOSSLESS_UNAVAILABLE", EN)
    fa = error_message("LOSSLESS_UNAVAILABLE", FA)

    assert "MP3 320" in en and "original" in en.lower()
    assert "MP3 320" in fa and "اصلی" in fa
