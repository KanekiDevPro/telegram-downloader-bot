"""The login wizard: what the jar is missing, and what to actually do about it.

The failure this exists for looks like a blocked IP but is not: a jar without
``LOGIN_INFO`` and a ``SAPISID`` cookie sends every request anonymously. The
wizard's job is to say which of the two traps is in play, offer only profiles that
really exist on this machine, and never claim a login that did not land — so these
tests pin the wording and the checks, not the export itself (that lives in
``tests/test_cookie_refresh.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from services import login_wizard
from services.extractor import CookieJarState

HEADER = "# Netscape HTTP Cookie File\n"


def _state(root: Path, **overrides: Any) -> CookieJarState:
    base = CookieJarState(
        path=root / "cookies.txt",
        kind="ok",
        size_bytes=512,
        exported_at=1_000.0,
        mount=None,
        writable=False,
        cookie_count=24,
        missing_login=(),
        copy_path=None,
        copy_refreshed_at=None,
        in_sync=True,
    )
    return CookieJarState(**(base.__dict__ | overrides))


def _logged_out_jar(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + ".youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tv\n", encoding="utf-8")
    return path


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two browsers exist here; every other one does not."""
    monkeypatch.setattr(
        login_wizard,
        "browser_profile_reachable",
        lambda spec: spec in {"chrome", "firefox"},
    )
    monkeypatch.setattr(
        login_wizard,
        "browser_profile_paths",
        lambda spec: (Path("/profiles") / spec / "Cookies",),
    )


# ---------------------------------------------------------------------------
# The diagnosis
# ---------------------------------------------------------------------------


def test_a_signed_in_jar_needs_nothing(tmp_path: Path) -> None:
    diagnosis = login_wizard.diagnose_from_state(_state(tmp_path))

    assert diagnosis.signed_in and diagnosis.usable
    assert "لاگین‌شده" in diagnosis.summary


def test_a_login_less_jar_is_the_whole_point(tmp_path: Path) -> None:
    diagnosis = login_wizard.diagnose_from_state(_state(tmp_path, missing_login=("LOGIN_INFO",)))

    assert diagnosis.usable and not diagnosis.signed_in
    assert "لاگین یوتیوب ندارد" in diagnosis.summary
    assert "LOGIN_INFO" in diagnosis.summary


def test_a_missing_jar_is_its_own_state(tmp_path: Path) -> None:
    diagnosis = login_wizard.diagnose_from_state(
        _state(tmp_path, path=tmp_path / "nope.txt", kind="missing", cookie_count=0)
    )

    assert not diagnosis.usable
    assert "خوانده نمی‌شود" in diagnosis.summary


def test_the_state_is_trusted_as_it_is(tmp_path: Path) -> None:
    """The extractor already read the file; the wizard is not a second opinion."""
    diagnosis = login_wizard.diagnose_from_state(
        _state(tmp_path, missing_login=("LOGIN_INFO", "SAPISID / __Secure-1PAPISID / __Secure-3PAPISID"))
    )

    assert diagnosis.missing_login == ("LOGIN_INFO", "SAPISID / __Secure-1PAPISID / __Secure-3PAPISID")
    assert not diagnosis.signed_in


# ---------------------------------------------------------------------------
# Which profiles exist here
# ---------------------------------------------------------------------------


def test_only_the_browsers_this_machine_has_are_offered(profiles: None) -> None:
    found = login_wizard.candidates()

    assert [candidate.spec for candidate in found] == ["chrome", "firefox"]
    assert all(candidate.paths for candidate in found)


def test_a_locked_profile_is_offered_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Windows browser whose cookies no outside tool can decrypt."""
    monkeypatch.setattr(login_wizard, "browser_profile_reachable", lambda spec: spec == "edge")
    monkeypatch.setattr(login_wizard, "browser_profile_paths", lambda spec: (Path("/x/Cookies"),))
    monkeypatch.setattr(login_wizard, "app_bound_encryption_active", lambda spec: True)

    (candidate,) = login_wizard.candidates()

    assert candidate.locked is True
    assert "App-Bound" in candidate.label, "the list says why it cannot be read"
    assert "yt-dlp" in candidate.label


def test_an_unknown_layout_is_not_offered_but_is_not_a_lie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` means \"cannot tell\": the wizard simply does not list it."""
    monkeypatch.setattr(login_wizard, "browser_profile_reachable", lambda spec: None)

    assert login_wizard.candidates() == ()


def test_a_container_offers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(login_wizard, "browser_profile_reachable", lambda spec: False)

    assert login_wizard.candidates() == ()


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def test_the_signed_in_jar_gets_the_ip_advice(tmp_path: Path) -> None:
    steps = login_wizard.next_steps(
        login_wizard.diagnose_from_state(_state(tmp_path)), have_profile=True
    )

    assert any("پروکسی" in step or "YTDLP_PROXY" in step for step in steps)
    assert any("/doctor" in step for step in steps)


def test_a_login_less_jar_gets_the_export_steps(tmp_path: Path) -> None:
    steps = login_wizard.next_steps(
        login_wizard.diagnose_from_state(_state(tmp_path, missing_login=("LOGIN_INFO",))),
        have_profile=True,
    )

    assert any("youtube.com" in step for step in steps), "sign in first"
    assert any("scripts/fix_login.py" in step for step in steps), "then run the wizard"
    assert not any("کانتینر" in step for step in steps), "the browser is here"


def test_without_a_browser_the_steps_say_which_machine(tmp_path: Path) -> None:
    steps = login_wizard.next_steps(
        login_wizard.diagnose_from_state(_state(tmp_path, missing_login=("LOGIN_INFO",))),
        have_profile=False,
    )

    assert any("کانتینر" in step for step in steps)


# ---------------------------------------------------------------------------
# What /fixlogin prints
# ---------------------------------------------------------------------------


def test_fixlogin_names_the_state_the_steps_and_the_traps(
    tmp_path: Path, profiles: None
) -> None:
    diagnosis = login_wizard.diagnose_from_state(_state(tmp_path, missing_login=("LOGIN_INFO",)))

    text = login_wizard.render_fixlogin(diagnosis, login_wizard.candidates())

    assert "راهنمای ورود واقعی یوتیوب" in text
    assert "پروفایل‌های پیدا‌شده" in text and "chrome" in text
    assert "دو تلهٔ همیشگی" in text
    assert "HttpOnly" in text
    assert "scripts/fix_login.py" in text


def test_fixlogin_does_not_lecture_a_healthy_jar(tmp_path: Path, profiles: None) -> None:
    text = login_wizard.render_fixlogin(
        login_wizard.diagnose_from_state(_state(tmp_path)), login_wizard.candidates()
    )

    assert "دو تلهٔ همیشگی" not in text, "nothing is missing, so there is nothing to warn about"
    assert "پروکسی" in text or "YTDLP_PROXY" in text


def test_fixlogin_escapes_what_it_prints_into_telegram() -> None:
    """A path is data; HTML in it must not become markup."""
    diagnosis = login_wizard.JarDiagnosis(
        path=Path("cookies<b>.txt"), kind="directory", cookies=0, missing_login=()
    )

    text = login_wizard.render_fixlogin(diagnosis)

    assert "<b>" not in text.split("راهنمای")[1], "only the heading may use markup"
    assert "cookies&lt;b&gt;.txt" in text


async def test_the_probe_is_the_watchers_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """One probe implementation, so the wizard and the alert cannot disagree."""
    seen: list[Any] = []

    async def fake_probe(extractor: Any, state: Any) -> str:
        seen.append(state)
        return "✅ تست شد"

    monkeypatch.setattr(login_wizard, "verify_export", fake_probe)
    state = _state(Path("/tmp"))

    verdict = await login_wizard.probe(object(), state)  # type: ignore[arg-type]

    assert verdict == "✅ تست شد" and seen == [state]
