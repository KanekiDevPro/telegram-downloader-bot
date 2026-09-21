"""The jar → cobalt cookie file bridge.

Cobalt's own contract is pinned here (a mapping of service → *list of strings*,
each a `Cookie:` header; its ``Cookie.fromString`` splits on ``'; '``), because
getting it wrong is silent: the file loads, and YouTube still answers
``error.api.youtube.login``. So are the two properties the operator relies on —
nothing this module did not write is ever replaced, and a generation timestamp
survives a restart (only a *content* change is a new export).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.config import Settings
from services import cobalt_cookies
from services.cobalt_cookies import (
    CobaltCookieState,
    cobalt_cookie_paths,
    cookie_header,
    read_state,
    restart_needed,
    sync_from_jar,
)

LOGIN_ROW = [
    ".youtube.com",
    "TRUE",
    "/",
    "TRUE",
    "0",
    "LOGIN_INFO",
    "AFmmF2swRQIg...",
]
SAPISID_ROW = [".youtube.com", "TRUE", "/", "TRUE", "0", "SAPISID", "abc/def"]
ANON_ROW = [".youtube.com", "TRUE", "/", "FALSE", "0", "VISITOR_INFO1_LIVE", "xyz"]
OTHER_SITE = [".instagram.com", "TRUE", "/", "TRUE", "0", "sessionid", "ig"]


def _jar(tmp_path: Path, rows: list[list[str]], name: str = "cookies.txt") -> Path:
    """A real Netscape jar, written the way the export scripts write one."""
    path = tmp_path / name
    lines = ["# Netscape HTTP Cookie File"]
    for domain, flag, cookie_path, secure, expires, key, value in rows:
        prefix = "#HttpOnly_" if secure == "TRUE" else ""
        lines.append("\t".join([f"{prefix}{domain}", flag, cookie_path, secure, expires, key, value]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _settings(tmp_path: Path, *, directory: Path | None = None, jar: Path | None = None) -> Settings:
    """A signed-in jar by default (LOGIN_INFO + SAPISID), like a real export."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        COOKIE_FILE=jar if jar is not None else _jar(tmp_path, [LOGIN_ROW, SAPISID_ROW]),
        COBALT_COOKIES_DIR=directory,
    )


# ---------------------------------------------------------------------------
# The header cobalt parses
# ---------------------------------------------------------------------------


def test_the_header_holds_the_session_cookies_only() -> None:
    header, used, skipped = cookie_header([LOGIN_ROW, SAPISID_ROW, ANON_ROW, OTHER_SITE])

    assert used == 3  # the instagram cookie is another service's business
    assert skipped == 0
    names = [part.split("=")[0] for part in header.split("; ")]
    # Session cookies first: cobalt only sends this header to YouTube, so the
    # cookies that *are* the login must not be the ones a peer drops.
    assert names[:2] == ["LOGIN_INFO", "SAPISID"]
    assert "VISITOR_INFO1_LIVE" in names
    assert "sessionid" not in header


def test_a_google_export_contributes_only_the_session_family() -> None:
    """A full Google jar is hundreds of cookies; only the login families belong."""
    noise = [".google.com", "TRUE", "/", "TRUE", "0", "NID", "tracking"]
    sid = [".google.com", "TRUE", "/", "TRUE", "0", "SID", "google-sid"]

    header, used, _ = cookie_header([noise, sid])

    assert used == 1
    assert header == "SID=google-sid"


def test_a_value_that_would_corrupt_the_rest_is_skipped() -> None:
    """Cobalt splits the string back with `'; '` — a value containing it would
    silently attach the remainder to the next cookie."""
    broken = [".youtube.com", "TRUE", "/", "TRUE", "0", "BROKEN", "a; b"]

    header, used, skipped = cookie_header([LOGIN_ROW, broken])

    assert used == 1 and skipped == 1
    assert "BROKEN" not in header


def test_the_most_specific_row_wins_a_duplicate() -> None:
    host = [".youtube.com", "TRUE", "/", "TRUE", "0", "SAPISID", "from-dot"]
    exact = ["youtube.com", "TRUE", "/", "TRUE", "0", "SAPISID", "from-exact"]

    header, used, _ = cookie_header([host, exact])

    assert used == 1
    assert header == "SAPISID=from-dot"  # first row wins, as a browser would send


# ---------------------------------------------------------------------------
# The file cobalt reads
# ---------------------------------------------------------------------------


def test_the_document_is_the_shape_cobalt_parses(tmp_path: Path) -> None:
    settings = _settings(tmp_path, directory=tmp_path / "cobalt")

    state = sync_from_jar(settings)

    assert state.written and state.cookie_count == 2  # LOGIN_INFO + SAPISID
    document = json.loads(Path(state.path).read_text(encoding="utf-8"))  # type: ignore[arg-type]
    assert list(document) == ["youtube"]
    # `VALID_SERVICES` + `cookies[service].some(c => typeof c !== 'string')`
    assert isinstance(document["youtube"], list)
    assert all(isinstance(entry, str) for entry in document["youtube"])
    assert document["youtube"][0].startswith("LOGIN_INFO=")


def test_a_second_run_is_not_a_new_export(tmp_path: Path) -> None:
    """A restart rewrites the same bytes — cobalt must not be asked to restart for
    that, and the stamp that proves what cobalt loaded must survive."""
    settings = _settings(tmp_path, directory=tmp_path / "cobalt")
    first = sync_from_jar(settings)

    again = sync_from_jar(settings)

    assert first.written and first.generated_at is not None
    assert again.written is False
    assert again.generated_at == first.generated_at
    assert again.cookie_count == first.cookie_count


def test_another_services_cookies_are_kept(tmp_path: Path) -> None:
    directory = tmp_path / "cobalt"
    directory.mkdir()
    theirs = {"twitter": ["auth_token=keep-me; ct0=keep-me-too"]}
    (directory / cobalt_cookies.COBALT_FILE_NAME).write_text(
        json.dumps(theirs), encoding="utf-8"
    )
    settings = _settings(tmp_path, directory=directory)

    state = sync_from_jar(settings)

    document = json.loads((directory / cobalt_cookies.COBALT_FILE_NAME).read_text(encoding="utf-8"))
    assert document["twitter"] == theirs["twitter"], "not ours to lose"
    assert document["youtube"]
    assert state.other_services == ("twitter",)


def test_a_file_we_did_not_write_is_reported_not_replaced(tmp_path: Path) -> None:
    """The cobalt docs' own example suggests a flat array — the shape its parser
    then rejects. Someone will follow it, and their file must survive us."""
    directory = tmp_path / "cobalt"
    directory.mkdir()
    theirs = '[{"name": "LOGIN_INFO", "value": "hand-made"}]'
    (directory / cobalt_cookies.COBALT_FILE_NAME).write_text(theirs, encoding="utf-8")
    settings = _settings(tmp_path, directory=directory)

    state = sync_from_jar(settings)

    assert state.written is False
    assert "فهرست" in state.reason
    assert (directory / cobalt_cookies.COBALT_FILE_NAME).read_text(encoding="utf-8") == theirs


def test_a_jar_without_youtube_cookies_writes_nothing(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, directory=tmp_path / "cobalt", jar=_jar(tmp_path, [OTHER_SITE], "other.txt")
    )

    state = sync_from_jar(settings)

    assert state.written is False
    assert state.cookie_count == 0
    assert "یوتیوبی" in state.reason


def test_an_empty_directory_setting_turns_it_off(tmp_path: Path) -> None:
    settings = _settings(tmp_path, directory=None)

    state = sync_from_jar(settings)
    path, _ = cobalt_cookie_paths(settings)

    assert path is None
    assert state.off and state.written is False
    assert "خالی" in state.describe()


def test_the_write_is_atomic(tmp_path: Path) -> None:
    """No half-written document: cobalt reads this file at startup and would
    either fail to parse it or load half a login."""
    directory = tmp_path / "cobalt"
    settings = _settings(tmp_path, directory=directory)

    sync_from_jar(settings)

    leftovers = [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_the_sidecar_records_what_was_written(tmp_path: Path) -> None:
    directory = tmp_path / "cobalt"
    settings = _settings(tmp_path, directory=directory)

    sync_from_jar(settings)

    sidecar = json.loads(
        (directory / cobalt_cookies.SIDECAR_FILE_NAME).read_text(encoding="utf-8")
    )
    assert sidecar["cookies"] == 2
    assert sidecar["missing_login"] == []
    assert sidecar["source"].endswith("cookies.txt")


def test_the_login_gap_is_reported_for_the_generated_file_too(tmp_path: Path) -> None:
    """The same rule the jar is checked with: LOGIN_INFO plus a SAPISID family."""
    settings = _settings(tmp_path, directory=tmp_path / "cobalt", jar=_jar(tmp_path, [ANON_ROW]))

    state = sync_from_jar(settings)

    assert state.written and state.cookie_count == 1
    assert "LOGIN_INFO" in state.missing_login


def test_reading_the_state_counts_the_file_cobalt_reads(tmp_path: Path) -> None:
    """The artifact is the evidence, not the source it came from."""
    directory = tmp_path / "cobalt"
    settings = _settings(tmp_path, directory=directory)
    sync_from_jar(settings)

    # The jar moves on (a new export) but the file has not been regenerated yet.
    changed = _jar(tmp_path, [LOGIN_ROW, SAPISID_ROW, ANON_ROW], "newer.txt")
    state = read_state(settings, jar_path=changed)

    assert state.cookie_count == 2, "the file still holds two cookies"
    assert state.written is False


# ---------------------------------------------------------------------------
# Is the running instance on this version?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("generated_at", "started_at", "expected"),
    [
        (100.0, 50.0, True),  # generated after it booted: it has the older one
        (50.0, 100.0, False),  # booted after: it read this version
        (99.5, 100.0, False),  # a fraction earlier is the version it loaded
        (None, 100.0, None),  # nothing generated by us
        (100.0, None, None),  # the instance did not say
    ],
)
def test_the_restart_verdict(
    generated_at: float | None, started_at: float | None, expected: bool | None
) -> None:
    assert restart_needed(generated_at, started_at) is expected


def test_cannot_tell_is_not_a_yes(tmp_path: Path) -> None:
    """A file nobody generated here is not evidence *for* a restart either — the
    report says which fact is missing instead of inventing one."""
    state = CobaltCookieState(path=tmp_path / "cookies.json", cookie_count=5)

    assert "هنوز نوشته نشده" in state.describe()
