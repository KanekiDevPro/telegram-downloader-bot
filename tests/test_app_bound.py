"""Windows App-Bound Encryption: the profile is here, the cookies are not readable.

Chrome 127+ (Edge, Brave and the rest followed) encrypt the key that protects the
cookie database with the browser's own identity, so yt-dlp's DPAPI path fails with
a generic "failed to load cookies" that reads like a missing profile. The key's
presence in ``Local State`` says so *before* an export is attempted — and the fix
is not in yt-dlp, it is an export with HttpOnly, or Firefox.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from services import cookie_refresh
from services.extractor import app_bound_encryption_active, browser_local_state

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="App-Bound Encryption is a Windows-only scheme (elsewhere the key opens)",
)


def _browser_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, locked: bool) -> None:
    """A fake home with an Edge profile whose Local State says what we say."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    user_data = tmp_path / "AppData/Local/Microsoft/Edge/User Data"
    (user_data / "Default/Network").mkdir(parents=True)
    (user_data / "Default/Network/Cookies").write_bytes(b"sqlite-ish")
    state: dict[str, object] = {"os_crypt": {"encrypted_key": "DPAPI..."}}
    if locked:
        state["os_crypt"] = {"app_bound_encrypted_key": "APPB..."}
    (user_data / "Local State").write_text(json.dumps(state), encoding="utf-8")


def test_a_locked_profile_is_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _browser_home(monkeypatch, tmp_path, locked=True)

    assert browser_local_state("edge:Default") is not None
    assert app_bound_encryption_active("edge:Default") is True


def test_an_older_profile_without_the_key_is_still_usable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The DPAPI key alone is what yt-dlp handles; the app-bound one is not."""
    _browser_home(monkeypatch, tmp_path, locked=False)

    assert app_bound_encryption_active("edge:Default") is False


def test_firefox_has_no_such_lock(tmp_path: Path) -> None:
    assert browser_local_state("firefox") is None
    assert app_bound_encryption_active("firefox:default-release") is False


def test_a_missing_or_unreadable_state_is_not_a_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A guess here would refuse a profile that might work: absence is ``False``."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        "services.extractor.browser_local_state", lambda spec: tmp_path / "Local State"
    )

    assert app_bound_encryption_active("edge:Default") is False  # no file at all
    (tmp_path / "Local State").write_text("{not json", encoding="utf-8")
    assert app_bound_encryption_active("edge:Default") is False


def test_the_lock_message_says_what_actually_works() -> None:
    """Not \"about:config somewhere\" — the two paths that get a login in."""
    message = cookie_refresh.app_bound_message("edge:Default")

    assert "App-Bound" in message
    assert "HttpOnly" in message, "the extension export that keeps the login rows"
    assert "Firefox" in message, "or the browser that has no such lock"
    assert "لاگین" in message, "and it is clear this is not about being signed in"


def test_an_unknown_spec_still_gets_a_readable_message() -> None:
    assert "not-a-browser" in cookie_refresh.app_bound_message("not-a-browser")
