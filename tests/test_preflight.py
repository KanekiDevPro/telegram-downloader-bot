"""Preflight: telling the user before the wait, but only when it is *known*.

A YouTube link on a host where anonymous requests are being refused fails after
the queue, after extraction and after the user's wait. Suspicion alone must not
refuse a link — most YouTube videos work anonymously — so these tests pin the two
different answers: a note on the queued message, and a refusal once the refusal
has actually been observed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from services import preflight
from services.preflight import Preflight, youtube_preflight

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str) -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name}\tvalue\n"


def _logged_out_jar(path: Path) -> Path:
    path.write_text(HEADER + _row("PREF") + _row("__Secure-3PSID"), encoding="utf-8")
    return path


def _logged_in_jar(path: Path) -> Path:
    path.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_leftover_evidence() -> Iterator[None]:
    """The evidence is process-wide; a test must not inherit another one's."""
    preflight.clear_anonymous_refusal()
    yield
    preflight.clear_anonymous_refusal()


def test_a_non_youtube_link_is_never_held_up(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    assert youtube_preflight("https://instagram.com/reel/1", jar) == Preflight("ok")
    assert youtube_preflight("https://www.tiktok.com/@x/video/1", None) == Preflight("ok")


def test_a_jar_that_signs_in_is_ok(tmp_path: Path) -> None:
    jar = _logged_in_jar(tmp_path / "cookies.txt")

    assert youtube_preflight("https://youtu.be/abc", jar).status == "ok"


def test_a_login_less_jar_only_warns_and_still_queues(tmp_path: Path) -> None:
    """Most videos extract anonymously; refusing on suspicion would be wrong."""
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    verdict = youtube_preflight("https://youtu.be/abc", jar, lang="fa")

    assert verdict.status == "risky"
    assert not verdict.refused
    assert verdict.message and "لاگین" in verdict.message
    assert "ادمین" in verdict.message


def test_the_warning_is_also_available_in_english(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    verdict = youtube_preflight("https://youtu.be/abc", jar, lang="en")

    assert verdict.status == "risky"
    assert "not signed in" in verdict.message
    assert "ادمین" not in verdict.message


def test_an_observed_refusal_refuses_the_next_link(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")
    preflight.note_anonymous_refusal()

    verdict = youtube_preflight("https://youtu.be/abc", jar, lang="fa")

    assert verdict.refused
    assert verdict.message and "دوباره بفرست" in verdict.message
    assert "ادمین" in verdict.message  # it is already being handled


def test_a_missing_jar_refuses_once_a_refusal_was_seen(tmp_path: Path) -> None:
    preflight.note_anonymous_refusal()

    assert youtube_preflight("https://youtu.be/abc", tmp_path / "cookies.txt").refused


def test_the_evidence_expires(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")
    # Anchored to the process clock, not to a literal: "long ago" must stay long
    # ago on a machine that booted two minutes ago.
    observed = time.monotonic() - preflight.EVIDENCE_TTL_S - 1
    preflight.note_anonymous_refusal(now=observed)

    assert preflight.refusal_is_recent(now=observed + preflight.EVIDENCE_TTL_S - 1)
    assert not preflight.refusal_is_recent(now=observed + preflight.EVIDENCE_TTL_S + 1)
    # ...and the preflight itself judges against that clock: expired evidence warns,
    # fresh evidence refuses.
    expired_at = observed + preflight.EVIDENCE_TTL_S + 1
    assert youtube_preflight("https://youtu.be/abc", jar, now=expired_at).status == "risky"
    assert youtube_preflight("https://youtu.be/abc", jar, now=observed + 1).status == "blocked"


def test_a_signed_in_jar_forgets_old_evidence(tmp_path: Path) -> None:
    """The refusal was about the jar before the operator fixed it."""
    jar = _logged_in_jar(tmp_path / "cookies.txt")
    preflight.note_anonymous_refusal()

    assert youtube_preflight("https://youtu.be/abc", jar).status == "ok"
    assert preflight.refusal_age_s() is None


def test_a_refusal_records_the_moment_it_happened() -> None:
    preflight.note_anonymous_refusal(now=500.0)

    assert preflight.refusal_age_s(now=530.0) == pytest.approx(30.0)


def test_a_working_probe_forgets_old_evidence() -> None:
    preflight.note_anonymous_refusal()

    preflight.clear_anonymous_refusal()

    assert not preflight.refusal_is_recent()
    assert preflight.refusal_age_s() is None


def test_an_observed_refusal_no_longer_refuses_when_a_fallback_can_serve_it(
    tmp_path: Path,
) -> None:
    """Refusing here would be true about yt-dlp and false about the user's file."""
    jar = _logged_out_jar(tmp_path / "cookies.txt")
    preflight.note_anonymous_refusal(now=time.monotonic())

    verdict = youtube_preflight(
        "https://youtu.be/abc", jar, fallback_available=True, lang="fa"
    )

    assert not verdict.refused
    assert verdict.status == "risky"
    assert "جایگزین" in verdict.message


def test_a_suspicion_names_the_fallback_too(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    verdict = youtube_preflight(
        "https://youtu.be/abc", jar, fallback_available=True, lang="fa"
    )

    assert verdict.status == "risky"
    assert "جایگزین" in verdict.message


def test_a_fallback_does_not_change_a_link_that_was_fine(tmp_path: Path) -> None:
    """A healthy jar is still ``ok`` — the note is only about trouble."""
    jar = _logged_in_jar(tmp_path / "cookies.txt")

    assert youtube_preflight("https://youtu.be/abc", jar, fallback_available=True) == Preflight("ok")
    assert youtube_preflight("https://instagram.com/reel/1", None, fallback_available=True) == Preflight(
        "ok"
    )


def test_the_window_is_long_enough_to_be_useful_and_short_enough_to_heal() -> None:
    assert 60.0 <= preflight.EVIDENCE_TTL_S <= 3600.0
