"""YouTube login-cookie inspection tests (offline).

An incomplete cookie jar is the failure that most looks like something else:
yt-dlp sends anonymous requests, YouTube answers "Sign in to confirm you're not
a bot", and the operator concludes the server's IP is banned. These tests pin the
inspection that tells the two apart, including the HttpOnly rows an exporter is
most likely to have dropped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from yt_dlp.utils import DownloadError

from services import cookie_refresh
from services.extractor import (
    ExtractorService,
    missing_youtube_login_cookies,
    read_netscape_cookie_names,
    youtube_login_hint,
)

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str, *, domain: str = ".youtube.com", http_only: bool = False, value: str = "v") -> str:
    prefix = "#HttpOnly_" if http_only else ""
    return f"{prefix}{domain}\tTRUE\t/\tFALSE\t2147483647\t{name}\t{value}\n"


def _jar(tmp_path: Path, *rows: str, name: str = "cookies.txt") -> Path:
    path = tmp_path / name
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading the jar
# ---------------------------------------------------------------------------

def test_http_only_rows_are_visible_by_name(tmp_path: Path) -> None:
    """http.cookiejar hides the #HttpOnly_ marker; the login cookies live there."""
    path = _jar(
        tmp_path,
        _row("SESSION"),
        _row("LOGIN_INFO", http_only=True),
        _row("SID", http_only=True, domain=".google.com"),
    )

    assert read_netscape_cookie_names(path) == {"SESSION", "LOGIN_INFO", "SID"}


def test_missing_or_empty_file_yields_no_names(tmp_path: Path) -> None:
    assert read_netscape_cookie_names(tmp_path / "absent.txt") == frozenset()
    assert read_netscape_cookie_names(None) == frozenset()


# ---------------------------------------------------------------------------
# What yt-dlp needs to consider a session signed in
# ---------------------------------------------------------------------------

def test_complete_login_reports_nothing_missing(tmp_path: Path) -> None:
    path = _jar(tmp_path, _row("LOGIN_INFO", http_only=True), _row("SAPISID", http_only=True))

    assert missing_youtube_login_cookies(path) == ()


NO_SAPISID = "SAPISID / __Secure-1PAPISID / __Secure-3PAPISID"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        # A jar that has everything except the login marker.
        ((_row("__Secure-3PSID"), _row("SAPISID")), ("LOGIN_INFO",)),
        # The real-world case: an exporter that kept only the *PSID variants.
        (
            (_row("__Secure-3PSID"), _row("__Secure-1PSID"), _row("__Secure-3PSIDTS")),
            ("LOGIN_INFO", NO_SAPISID),
        ),
        ((), ("LOGIN_INFO", NO_SAPISID)),
    ],
)
def test_incomplete_logins_list_what_is_missing(
    tmp_path: Path, rows: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    path = _jar(tmp_path, *rows)

    assert missing_youtube_login_cookies(path) == expected


def test_hint_is_silent_unless_the_jar_loads_but_cannot_sign_in(tmp_path: Path) -> None:
    assert youtube_login_hint(None) is None
    assert youtube_login_hint(tmp_path / "absent.txt") is None
    assert youtube_login_hint(_jar(tmp_path, name="empty.txt")) is None  # header only
    assert youtube_login_hint(_jar(tmp_path, _row("LOGIN_INFO"), _row("SAPISID"))) is None

    hint = youtube_login_hint(_jar(tmp_path, _row("__Secure-3PSID"), name="partial.txt"))

    assert hint is not None
    assert "LOGIN_INFO" in hint
    assert "Sign in to confirm" in hint


# ---------------------------------------------------------------------------
# Wiring into the service
# ---------------------------------------------------------------------------

def test_service_reports_whether_the_jar_signs_youtube_in(tmp_path: Path) -> None:
    signed_in = ExtractorService(
        tmp_path,
        cookie_file=_jar(tmp_path, _row("LOGIN_INFO"), _row("SAPISID"), name="signed-in.txt"),
    )
    anonymous = ExtractorService(
        tmp_path, cookie_file=_jar(tmp_path, _row("__Secure-3PSID"), name="anonymous.txt")
    )

    assert signed_in.using_cookies is True and signed_in.youtube_login_ready is True
    assert anonymous.using_cookies is True and anonymous.youtube_login_ready is False
    assert ExtractorService(
        tmp_path, cookie_file=tmp_path / "absent.txt"
    ).youtube_login_ready is False


def test_blocked_download_logs_the_login_problem_not_an_ip_ban(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The whole point of the hint: point at the cookies before the operator buys a proxy."""
    extractor = ExtractorService(tmp_path, cookie_file=_jar(tmp_path, _row("__Secure-3PSID")))
    blocked = DownloadError(
        "ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
    )

    with caplog.at_level("WARNING"):
        error = extractor._translate(blocked)

    assert error.code == "EXTRACTOR_BLOCKED"
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "no YouTube login" in message
    assert "LOGIN_INFO" in message


def _load_exporter() -> ModuleType:
    """Import scripts/export_cookies.py without turning scripts/ into a package."""
    spec = importlib.util.spec_from_file_location(
        "export_cookies", Path(__file__).resolve().parent.parent / "scripts" / "export_cookies.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exporter_flags_a_jar_without_a_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The export script must not hand back a jar that silently cannot log in."""
    exporter = _load_exporter()

    output = tmp_path / "cookies.txt"

    def fake_export(spec: str, path: Path, *, verbose: bool = False) -> int:
        path.write_text(HEADER + _row("__Secure-3PSID") + _row("__Secure-3PAPISID"), encoding="utf-8")
        return 2

    monkeypatch.setattr(exporter, "export_jar_from_browser", fake_export)
    monkeypatch.setattr(exporter, "force_utf8_console", lambda *_: None)

    code = exporter.main(["--browser", "chrome", "--output", str(output)])

    assert code == 3  # written, but unusable as a YouTube login
    assert "no YouTube login" in capsys.readouterr().out


def test_exporter_is_happy_when_the_login_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = _load_exporter()

    output = tmp_path / "cookies.txt"

    def fake_export(spec: str, path: Path, *, verbose: bool = False) -> int:
        path.write_text(
            HEADER + _row("LOGIN_INFO", http_only=True) + _row("SAPISID", http_only=True),
            encoding="utf-8",
        )
        return 2

    monkeypatch.setattr(exporter, "export_jar_from_browser", fake_export)
    monkeypatch.setattr(exporter, "force_utf8_console", lambda *_: None)

    assert exporter.main(["--browser", "chrome", "--output", str(output)]) == 0


def test_exporter_replaces_dockers_empty_placeholder_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A first-ever export finds an empty ``cookies.txt/`` where the mounted file
    should be; writing must work instead of raising IsADirectoryError."""
    exporter = _load_exporter()
    placeholder = tmp_path / "cookies.txt"
    placeholder.mkdir()

    def fake_export(spec: str, path: Path, *, verbose: bool = False) -> int:
        # The export core now lives in the service layer (the script only wraps it).
        cookie_refresh.clear_docker_placeholder(path)
        path.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
        return 2

    monkeypatch.setattr(exporter, "export_jar_from_browser", fake_export)
    monkeypatch.setattr(exporter, "force_utf8_console", lambda *_: None)

    with caplog.at_level("INFO"):
        code = exporter.main(["--browser", "chrome", "--output", str(placeholder)])

    assert code == 0
    assert placeholder.is_file()
    assert "was an empty directory" in caplog.text


def test_exporter_refuses_to_clobber_a_real_directory(tmp_path: Path) -> None:
    directory = tmp_path / "cookies.txt"
    directory.mkdir()
    (directory / "keep.txt").write_text("not a cookie", encoding="utf-8")

    with pytest.raises(cookie_refresh.ExportError):
        cookie_refresh.clear_docker_placeholder(directory)

    assert (directory / "keep.txt").exists()


def test_blocked_download_without_cookies_still_lists_every_fix(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extractor = ExtractorService(tmp_path, cookie_file=tmp_path / "absent.txt")
    blocked = DownloadError("ERROR: [youtube] x: Sign in to confirm you’re not a bot")

    with caplog.at_level("WARNING"):
        extractor._translate(blocked)

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "COOKIE_FILE" in message
    assert "YTDLP_POT_PROVIDER_URL" in message
    assert "YTDLP_PROXY" in message
