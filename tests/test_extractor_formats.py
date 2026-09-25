"""Extractor configuration tests: format preference and cookie handling.

Everything here is offline. The format strings are validated with yt-dlp's own
parser, which is the only reliable way to keep a hand-written selector honest —
an invalid one is only discovered when a user sends a link.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yt_dlp

from core.config import BASE_DIR, Settings
from services.extractor import (
    AUDIO_FORMAT_SELECTOR,
    MERGE_OUTPUT_FORMAT,
    VIDEO_FORMAT_SELECTOR,
    BrowserSpecError,
    ExtractorService,
    YdlLogAdapter,
    classify_ydl_warning,
    cookie_jar_is_usable,
    detect_js_runtimes,
    format_selector,
    js_runtime_boot_line,
    parse_browser_spec,
    pot_plugin_installed,
    pot_plugin_version,
)


def _parser() -> yt_dlp.YoutubeDL:
    return yt_dlp.YoutubeDL({"quiet": True})


def _extractor(**kwargs: object) -> ExtractorService:
    return ExtractorService(BASE_DIR / "downloads", **kwargs)  # type: ignore[arg-type]


def test_aria2c_is_the_external_downloader_with_the_measured_args() -> None:
    """Eight connections per plain-file download, in the one spelling the
    installed yt-dlp reads: its argument lookup tries the downloader's own key
    and then ``default`` (``cli_configuration_args``), and a host without the
    binary is detected and keeps yt-dlp's own downloader (``can_download``)."""
    opts = _extractor()._base_opts(extract_only=True)
    assert opts["external_downloader"] == "aria2c"
    assert opts["external_downloader_args"] == {
        "default": ["-c", "-j", "8", "-x", "8", "-s", "8", "-k", "1M"],
    }


def test_the_image_ships_the_downloader_the_options_name() -> None:
    dockerfile = (BASE_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "aria2" in dockerfile, "the opts name aria2c; the image must carry it"


def test_segmented_sources_fetch_eight_fragments_at_a_time() -> None:
    """YouTube hands yt-dlp DASH/HLS — fragmented streams that never reach an
    external downloader. Their speed-up is parallel fragment fetching."""
    opts = _extractor()._base_opts(extract_only=True)
    assert opts["concurrent_fragment_downloads"] == 8


def test_proxy_is_passed_to_yt_dlp_only_when_set() -> None:
    plain = _extractor()._base_opts(extract_only=True)
    assert "proxy" not in plain
    assert _extractor().using_proxy is False

    proxied = _extractor(proxy="socks5://127.0.0.1:1080")._base_opts(extract_only=True)
    assert proxied["proxy"] == "socks5://127.0.0.1:1080"
    assert _extractor(proxy="  ").using_proxy is False


# ---------------------------------------------------------------------------
# PO token provider
# ---------------------------------------------------------------------------

def test_no_extractor_args_without_provider() -> None:
    extractor = _extractor()
    assert extractor.using_pot_provider is False
    assert extractor.extractor_args == {}
    assert "extractor_args" not in extractor._base_opts(extract_only=True)


def test_provider_url_becomes_bgutil_extractor_args() -> None:
    extractor = _extractor(pot_provider_url="http://pot-provider:4416/")  # trailing slash tolerated
    assert extractor.using_pot_provider is True
    opts = extractor._base_opts(extract_only=True)
    # Exactly the key yt-dlp's --extractor-args "youtubepot-bgutilhttp:base_url=…" sets.
    assert opts["extractor_args"] == {"youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]}}


def test_bgutil_plugin_is_installed() -> None:
    # requirements.txt ships it: without the plugin a configured provider URL is inert.
    assert pot_plugin_installed() is True


def test_reading_the_plugin_version_does_not_import_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The version comes from the distribution, on purpose.

    Importing the plugin registers it with yt-dlp's provider registry, and a second
    registration (yt-dlp's own loader does the first) fails with "already
    registered" — an error about a plugin that works, raised inside the very process
    that is about to download. So the read must not import anything: proven by
    refusing every import and still getting the number.
    """

    def _refuse(name: str) -> object:
        raise AssertionError(f"pot_plugin_version() must not import {name}")

    monkeypatch.setattr(importlib, "import_module", _refuse)

    # Installed in this environment, so the version has to come back anyway.
    assert pot_plugin_version()


# ---------------------------------------------------------------------------
# JavaScript runtime (yt-dlp needs one for YouTube)
# ---------------------------------------------------------------------------

def test_js_runtime_can_be_disabled() -> None:
    assert detect_js_runtimes("none") == {}
    assert detect_js_runtimes("") == {}
    assert "js_runtimes" not in _extractor(js_runtime="none")._base_opts(extract_only=True)


def test_js_runtime_explicit_name_and_path() -> None:
    assert detect_js_runtimes("node") == {"node": {"path": "node"}}
    assert detect_js_runtimes("node:/opt/bin/node") == {"node": {"path": "/opt/bin/node"}}
    # quickjs is driven through its qjs executable.
    assert detect_js_runtimes("quickjs") == {"quickjs": {"path": "qjs"}}


def test_js_runtime_autodetect_prefers_deno(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.extractor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in {"deno", "node"} else None,
    )
    assert detect_js_runtimes("auto") == {"deno": {"path": "/usr/bin/deno"}}


def test_js_runtime_autodetect_falls_back_to_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.extractor.shutil.which", lambda name: "/usr/bin/node" if name == "node" else None
    )
    assert detect_js_runtimes() == {"node": {"path": "/usr/bin/node"}}


def test_js_runtime_autodetect_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("services.extractor.shutil.which", lambda name: None)
    assert detect_js_runtimes("auto") == {}
    assert _extractor(js_runtime="none").js_runtime_name == "none"


def test_detected_runtime_reaches_yt_dlp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.extractor.shutil.which", lambda name: "/usr/bin/deno" if name == "deno" else None
    )
    extractor = _extractor()
    assert extractor.js_runtime_name == "deno"
    assert extractor._base_opts(extract_only=True)["js_runtimes"] == {"deno": {"path": "/usr/bin/deno"}}


def test_missing_js_runtime_names_the_consequence_not_just_the_absence() -> None:
    """"Not found" alone sends nobody for Deno; the sentence must say what it
    costs (unsolvable n challenges, missing formats) and what fixes it."""
    warning = js_runtime_boot_line({})
    assert warning is not None
    assert "YTDLP_JS_RUNTIME" in warning and "challenge" in warning
    assert js_runtime_boot_line({"deno": {"path": "/usr/bin/deno"}}) is None


# ---------------------------------------------------------------------------
# EJS challenge solvers (n/sig): script sources and warning routing
# ---------------------------------------------------------------------------

def test_remote_components_reach_yt_dlp_in_its_list_spelling(tmp_path: Path) -> None:
    """2026.08.19 takes ``['ejs:github']`` / ``['ejs:npm']`` — its own
    ``--remote-components`` values. The dict form (``{'ejs': 'github'}``) that
    some guides show is silently discarded by this version — configured in
    appearance only, which is the failure mode this test exists to prevent."""
    extractor = _extractor(remote_components=["ejs:GitHub", " ejs:github ", "ejs:npm"])

    opts = extractor._base_opts(extract_only=True)

    assert opts["remote_components"] == ["ejs:github", "ejs:npm"], (
        "lower-cased, de-duplicated, in order"
    )


def test_remote_components_key_is_absent_when_unconfigured(tmp_path: Path) -> None:
    assert "remote_components" not in _extractor()._base_opts(extract_only=True)


def test_remote_components_setting_parses_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).ytdlp_remote_components == ["ejs:github"]  # type: ignore[call-arg]
    monkeypatch.setenv("YTDLP_REMOTE_COMPONENTS", "ejs:github, ejs:npm")
    assert Settings(_env_file=None).ytdlp_remote_components == ["ejs:github", "ejs:npm"]  # type: ignore[call-arg]
    monkeypatch.setenv("YTDLP_REMOTE_COMPONENTS", "")
    assert Settings(_env_file=None).ytdlp_remote_components == []  # type: ignore[call-arg]


def test_generated_opts_route_warnings_instead_of_dropping_them(tmp_path: Path) -> None:
    """``no_warnings`` used to swallow the *only* record of why a challenge
    solve or a solver-script fetch failed — the diagnostics live in those
    warnings, so they are routed into our log instead (tagged by cause)."""
    opts = _extractor()._base_opts(extract_only=True)

    assert "no_warnings" not in opts
    assert isinstance(opts["logger"], YdlLogAdapter)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("n challenge solving failed: Some formats may be missing.", "YTDLP_CHALLENGE"),
        ("Signature solving failed: Some formats may be missing.", "YTDLP_CHALLENGE"),
        ("[youtube] Failed to download challenge solver lib script", "YTDLP_REMOTE_COMPONENTS"),
        ("No usable challenge solver lib script available", "YTDLP_EJS"),
        ("Failed to load challenge solver core script from python package: boom", "YTDLP_EJS"),
        ("Failed to load cookies from /cookies/cookies.txt: bad", "YTDLP_COOKIES"),
        ("No supported JavaScript runtime found", "YTDLP_JS_RUNTIME"),
        ("Requested format is not available", "YTDLP"),
    ],
)
def test_yt_dlp_warnings_are_classified_by_cause(text: str, code: str) -> None:
    assert classify_ydl_warning(text) == code


def test_ydl_log_adapter_tags_every_level(caplog: pytest.LogCaptureFixture) -> None:
    adapter = YdlLogAdapter()
    with caplog.at_level("DEBUG"):
        adapter.warning("n challenge solving failed: Some formats may be missing.")
        adapter.error("boom")
        adapter.debug("quiet detail")

    messages = [record.getMessage() for record in caplog.records]
    assert any("[YTDLP_CHALLENGE]" in message for message in messages)
    assert any("[YTDLP]" in message and "boom" in message for message in messages)
    assert any("quiet detail" in message for message in messages)


# ---------------------------------------------------------------------------
# COOKIES_FROM_BROWSER specs
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("chrome", ("chrome", None, None, None)),
        ("Firefox", ("firefox", None, None, None)),
        ("chrome:Profile 2", ("chrome", "Profile 2", None, None)),
        ("chrome+gnomekeyring:Default", ("chrome", "Default", "GNOMEKEYRING", None)),
        ("firefox:default-release::Meta", ("firefox", "default-release", None, "Meta")),
    ],
)
def test_browser_spec_is_parsed_like_yt_dlp(
    spec: str, expected: tuple[str, str | None, str | None, str | None]
) -> None:
    assert parse_browser_spec(spec) == expected


@pytest.mark.parametrize("spec", ["chrome", "Firefox", "chrome:Profile 2", "edge:Profile 1:ignored", "brave"])
def test_browser_spec_matches_yt_dlp_own_parser(spec: str) -> None:
    """The bot must accept exactly what ``--cookies-from-browser`` accepts."""
    from yt_dlp import parse_options

    theirs = parse_options(["--cookies-from-browser", spec]).options.cookiesfrombrowser
    assert parse_browser_spec(spec) == theirs


@pytest.mark.parametrize("spec", ["", "notabrowser", "chrome+notakeyring", "+", ":profile"])
def test_browser_spec_rejects_garbage(spec: str) -> None:
    with pytest.raises(BrowserSpecError):
        parse_browser_spec(spec)


def test_unusable_browser_spec_is_ignored_not_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    # yt-dlp raises CookieLoadError for *every* download when the profile cannot be
    # read, so a spec that fails the probe must never reach the options.
    extractor = _extractor(cookies_from_browser="chrome")
    monkeypatch.setattr("services.extractor.browser_cookie_jar_is_usable", lambda spec: False)
    extractor._browser_cookies_ok = None

    assert extractor.using_browser_cookies is False
    assert "cookiesfrombrowser" not in extractor._base_opts(extract_only=True)


def test_usable_browser_spec_is_passed_as_a_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _extractor(cookies_from_browser="chrome:Profile 2")
    monkeypatch.setattr("services.extractor.browser_cookie_jar_is_usable", lambda spec: True)
    extractor._browser_cookies_ok = None

    assert extractor.using_browser_cookies is True
    assert extractor._base_opts(extract_only=True)["cookiesfrombrowser"] == (
        "chrome",
        "Profile 2",
        None,
        None,
    )


def test_video_selector_is_valid_yt_dlp_syntax() -> None:
    _parser().build_format_selector(VIDEO_FORMAT_SELECTOR)


def test_audio_selector_is_valid_yt_dlp_syntax() -> None:
    _parser().build_format_selector(AUDIO_FORMAT_SELECTOR)


def test_hevc_is_preferred_over_avc() -> None:
    assert VIDEO_FORMAT_SELECTOR.index("hev") < VIDEO_FORMAT_SELECTOR.index("avc")


def test_blank_proxy_setting_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YTDLP_PROXY", "   ")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert _extractor(proxy=settings.ytdlp_proxy).using_proxy is False


def test_selectors_merge_telegram_friendly_containers() -> None:
    assert "[ext=mp4]" in VIDEO_FORMAT_SELECTOR
    assert "[ext=m4a]" in VIDEO_FORMAT_SELECTOR
    assert MERGE_OUTPUT_FORMAT.startswith("mp4")


def test_format_selector_dispatch() -> None:
    assert format_selector("video") == VIDEO_FORMAT_SELECTOR
    assert format_selector("audio") == AUDIO_FORMAT_SELECTOR


def test_base_opts_use_selector_per_media_type(tmp_path: Path) -> None:
    extractor = ExtractorService(tmp_path)
    assert extractor._base_opts(extract_only=True, media_format="audio")["format"] == (
        AUDIO_FORMAT_SELECTOR
    )
    video_opts = extractor._base_opts(extract_only=False, media_format="video")
    assert video_opts["format"] == VIDEO_FORMAT_SELECTOR
    assert video_opts["merge_output_format"] == MERGE_OUTPUT_FORMAT
    assert video_opts["noplaylist"] is True


def test_metadata_probe_still_skips_download(tmp_path: Path) -> None:
    assert ExtractorService(tmp_path)._base_opts(extract_only=True)["skip_download"] is True


NETSCAPE_HEADER = "# Netscape HTTP Cookie File\n"
NETSCAPE_ROW = ".example.com\tTRUE\t/\tFALSE\t2147483647\tSESSION\tabc123\n"


def test_valid_cookie_jar_is_passed_to_yt_dlp(tmp_path: Path) -> None:
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(NETSCAPE_HEADER + NETSCAPE_ROW, encoding="utf-8")

    extractor = ExtractorService(tmp_path, cookie_file=cookie)
    handed = Path(extractor._base_opts(extract_only=True)["cookiefile"])

    assert extractor.using_cookies is True
    # yt-dlp gets a writable copy, never the jar itself (see test_cookie_mount.py:
    # it rewrites the file on close, which a read-only mount forbids).
    assert handed != cookie
    assert handed.read_text(encoding="utf-8") == cookie.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("empty", ""),
        ("header only", NETSCAPE_HEADER),
        ("not a jar at all", "just some random text\n"),
    ],
)
def test_unusable_cookie_files_are_ignored(tmp_path: Path, name: str, content: str) -> None:
    """yt-dlp raises DownloadError on an empty/malformed jar — one bad file would
    otherwise break *every* download, so it must never reach the extractor."""
    cookie = tmp_path / f"{name.replace(' ', '_')}.txt"
    cookie.write_text(content, encoding="utf-8")

    extractor = ExtractorService(tmp_path, cookie_file=cookie)

    assert extractor.using_cookies is False
    assert "cookiefile" not in extractor._base_opts(extract_only=True)


def test_missing_cookie_file_is_ignored(tmp_path: Path) -> None:
    extractor = ExtractorService(tmp_path, cookie_file=tmp_path / "nope.txt")

    assert extractor.using_cookies is False
    assert "cookiefile" not in extractor._base_opts(extract_only=True)


def test_directory_named_cookies_txt_is_ignored(tmp_path: Path) -> None:
    """A Docker bind mount of a missing file creates a directory — not a jar."""
    directory = tmp_path / "cookies.txt"
    directory.mkdir()

    extractor = ExtractorService(tmp_path, cookie_file=directory)

    assert extractor.using_cookies is False
    assert "cookiefile" not in extractor._base_opts(extract_only=True)


def test_cookie_warning_is_emitted_once_per_path(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    extractor = ExtractorService(tmp_path, cookie_file=tmp_path / "missing.txt")

    with caplog.at_level("WARNING"):
        extractor._base_opts(extract_only=True)
        extractor._base_opts(extract_only=True)

    warnings = [r for r in caplog.records if "COOKIE_FILE" in r.getMessage()]
    assert len(warnings) == 1


def test_cookie_jar_is_usable_helper(tmp_path: Path) -> None:
    valid = tmp_path / "valid.txt"
    valid.write_text(NETSCAPE_HEADER + NETSCAPE_ROW, encoding="utf-8")

    assert cookie_jar_is_usable(valid) is True
    assert cookie_jar_is_usable(None) is False
    assert cookie_jar_is_usable(tmp_path / "absent.txt") is False


def test_cookie_file_defaults_to_project_root() -> None:
    # Dropping cookies.txt next to the bot is enough; no config needed.
    assert Settings(_env_file=None).cookie_file == BASE_DIR / "cookies.txt"  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["", "none", "null", "-"])
def test_cookie_file_can_be_disabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("COOKIE_FILE", value)
    assert Settings(_env_file=None).cookie_file is None  # type: ignore[call-arg]
