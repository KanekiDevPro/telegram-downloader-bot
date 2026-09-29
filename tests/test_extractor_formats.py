"""Extractor configuration tests: format preference and cookie handling.

Everything here is offline. The format strings are validated with yt-dlp's own
parser, which is the only reliable way to keep a hand-written selector honest —
an invalid one is only discovered when a user sends a link.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yt_dlp

from core.config import BASE_DIR, Settings
from services.extractor import (
    AUDIO_FORMAT_SELECTOR,
    MERGE_OUTPUT_FORMAT,
    VIDEO_FORMAT_SELECTOR,
    BrowserSpecError,
    ExtractionError,
    ExtractorService,
    MediaInfo,
    YdlLogAdapter,
    classify_ydl_warning,
    cookie_jar_is_usable,
    detect_js_runtimes,
    format_selector,
    js_runtime_boot_line,
    parse_browser_spec,
    pot_plugin_installed,
    pot_plugin_version,
    quality_label_p,
    selected_streams,
    video_options,
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

def test_download_mechanics_are_always_on_and_spoofing_is_not() -> None:
    """Without a provider and without a client list, yt-dlp is told only *how*
    to download (the dashy switch pinned below) — never *who* to be."""
    extractor = _extractor()
    assert extractor.using_pot_provider is False
    assert extractor.extractor_args == {"youtube": {"formats": ["dashy"]}}
    assert extractor._base_opts(extract_only=True)["extractor_args"] == {
        "youtube": {"formats": ["dashy"]}
    }


def test_provider_url_becomes_bgutil_extractor_args() -> None:
    extractor = _extractor(pot_provider_url="http://pot-provider:4416/")  # trailing slash tolerated
    assert extractor.using_pot_provider is True
    opts = extractor._base_opts(extract_only=True)
    # Exactly the key yt-dlp's --extractor-args "youtubepot-bgutilhttp:base_url=…" sets.
    assert opts["extractor_args"] == {
        "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]},
        "youtube": {"formats": ["dashy"]},
    }


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


# ---------------------------------------------------------------------------
# Throughput tuning (the 30-second delivery budget)
# ---------------------------------------------------------------------------

def test_socket_buffer_and_http_chunk_size_are_tuned() -> None:
    """Two yt-dlp knobs that decide how fast bytes actually arrive.

    ``buffersize`` is yt-dlp's own HTTP downloader read block
    (``downloader/http.py``: ``ctx.block_size`` — its default is 1024 bytes per
    socket read, a syscall per kilobyte on a link that moves hundreds of
    megabytes), and ``http_chunk_size`` splits a response into 10 MB ranged
    requests, which both raises single-connection throughput and dodges the
    per-request CDN throttling Google Video applies to long transfers. These
    only reach fragmented and non-external-downloader fetches — aria2c carries
    plain files (pinned above) — so together the three knobs cover every shape.
    """
    opts = _extractor()._base_opts(extract_only=True)

    assert opts["buffersize"] == 1048576, "1 MB read blocks, not 1 KB"
    assert opts["http_chunk_size"] == 10485760, "10 MB ranged chunks"


def test_the_tuning_is_the_same_for_real_downloads() -> None:
    """Extract-only opts are what the probes see; the download must not drift."""
    opts = _extractor()._base_opts(extract_only=False, media_format="video")

    assert opts["buffersize"] == 1048576
    assert opts["http_chunk_size"] == 10485760
    assert opts["concurrent_fragment_downloads"] == 8


# ---------------------------------------------------------------------------
# Which downloader actually fetches (verified against the installed yt-dlp)
# ---------------------------------------------------------------------------

def test_the_dashy_switch_lands_youtube_streams_in_the_fragment_downloader() -> None:
    """YouTube streams are plain ``https`` URLs, and two measured facts about
    that shape are why ``formats=dashy`` is part of every request:

    * the external downloader grabs plain ``https`` and never looks at the
      per-format ``downloader_options`` the YouTube extractor sets (its 10 MB
      ranged-request throttle fix is silently ignored — pinned below);
    * aria2c requests the URL without the ``&range`` the YouTube player appends
      to every playback request, and googlevideo delays and throttles requests
      lacking it (yt-dlp/yt-dlp#6400).

    ``formats=dashy`` makes the extractor emit the same stream as 10 MB ranged
    fragments *carrying* ``&range``, under ``http_dash_segments`` — the fragment
    downloader, where ``concurrent_fragment_downloads: 8`` actually fires.
    Eight parallel player-shaped ranged requests, not eight plain GETs on a
    throttled URL. This is what took a production job's download stage from
    50.4 s to streaming-speed.
    """
    from yt_dlp.downloader import get_suitable_downloader
    from yt_dlp.downloader.dash import DashSegmentsFD

    args = _extractor()._base_opts(extract_only=True)["extractor_args"]
    assert args["youtube"]["formats"] == ["dashy"]

    # The shape the dashy switch actually emits (yt-dlp's ``build_fragments``):
    # a ``http_dash_segments`` format whose fragments are 10 MB ranged requests.
    info = {
        "protocol": "http_dash_segments",
        "fragments": [
            {"url": "https://rr1---sn-x.googlevideo.com/videoplayback?a=b", "range": "0-1048575"}
        ],
        "ext": "mp4",
    }
    assert get_suitable_downloader(info, {"external_downloader": "aria2c"}) is DashSegmentsFD


def test_aria2c_takes_plain_https_and_ignores_the_extractor_s_chunking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The routing fact above, spelled out: ``downloader_options`` never
    disqualify an external downloader (``ExternalFD.supports`` never reads it),
    so a YouTube ``https`` format handed to aria2c loses the extractor's chunked
    ranged-request throttling fix entirely."""
    from yt_dlp.downloader import get_suitable_downloader
    from yt_dlp.downloader.external import Aria2cFD

    monkeypatch.setattr(Aria2cFD, "available", classmethod(lambda cls, path=None: True))
    info = {
        "url": "https://example.com/file.mp4",
        "protocol": "https",
        "ext": "mp4",
        "downloader_options": {"http_chunk_size": 10485760},
    }

    assert Aria2cFD.supports(info) is True, "the per-format chunking is ignored by design"
    assert get_suitable_downloader(info, {"external_downloader": "aria2c"}) is Aria2cFD


def test_the_throughput_knobs_are_the_names_the_installed_yt_dlp_reads() -> None:
    """A yt-dlp upgrade that renames a knob must fail here — not silently
    download with stock 1 KB reads and sequential fragments."""
    import inspect

    from yt_dlp.downloader import fragment as fragment_module
    from yt_dlp.downloader import http as http_module

    http_src = inspect.getsource(http_module)
    assert "self.params.get('buffersize', 1024)" in http_src
    assert "self.params.get('http_chunk_size')" in http_src
    assert "concurrent_fragment_downloads" in inspect.getsource(fragment_module)


def test_the_intake_probe_is_a_metadata_probe_not_a_download_dry_run() -> None:
    """Eleven seconds from link to menu is the probe paying for checks the
    menu never asked for. The stock ``check_formats`` stance fires a HEAD/GET
    at every untested format URL before the list is even reported; a menu only
    needs to know which formats exist. The probe is metadata only — nothing is
    downloaded (``skip_download``), no comment section, no subtitle fetch, no
    format validation; the manifests and storyboards a player response lists
    are listings, not fetches. The download path keeps yt-dlp's own judgement:
    its pre-flight is the one that must catch a dead URL *before* a job starts."""
    probe = _extractor()._base_opts(extract_only=True)

    assert probe["skip_download"] is True
    assert probe["check_formats"] is False
    assert probe["getcomments"] is False
    assert probe["writesubtitles"] is False
    assert probe["writeautomaticsub"] is False

    fetch = _extractor()._base_opts(extract_only=False, media_format="video", quality="")
    assert fetch.get("check_formats") is not False, "the download keeps yt-dlp's pre-flight"


def test_concurrent_youtube_dl_construction_registers_the_plugins_once(tmp_path: Path) -> None:
    """The live crash: ``AssertionError: PoTokenProvider BgUtilHTTP already
    registered`` — one registry entry, two claimants, from two raced probes.
    yt-dlp loads its plugins on the *first* ``YoutubeDL`` construction, a
    check-then-act on ``all_plugins_loaded`` with no lock, and its loader
    executes plugin module bodies outside the import system's locks — so two
    constructions in the same moment both run the bgutil registration and the
    second dies on the assertion (taking the PO-token route with it, and the
    intake's 9 s of retries after it). Warming the registry once and
    constructing under a lock makes the load single-flight. Reproduced in a
    fresh interpreter where the registry really is cold: the loader must run
    exactly once and never write an assertion."""
    script = tmp_path / "race.py"
    script.write_text(
        "import importlib, sys, threading, time\n"
        f"sys.path.insert(0, {str(BASE_DIR)!r})\n"
        "ydl_module = importlib.import_module('yt_dlp.YoutubeDL')\n"
        "loads = []\n"
        "original = ydl_module.load_all_plugins\n"
        "def counting():\n"
        "    loads.append(1)\n"
        "    time.sleep(0.1)\n"
        "    original()\n"
        "ydl_module.load_all_plugins = counting\n"
        "extractor = importlib.import_module('services.extractor')\n"
        "barrier = threading.Barrier(4)\n"
        "def build():\n"
        "    barrier.wait()\n"
        "    with extractor._new_ydl({'quiet': True}):\n"
        "        pass\n"
        "threads = [threading.Thread(target=build) for _ in range(4)]\n"
        "for thread in threads:\n"
        "    thread.start()\n"
        "for thread in threads:\n"
        "    thread.join()\n"
        "print('LOADS', len(loads))\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr
    assert "LOADS 1" in result.stdout, f"one plugin load, ever: {result.stdout!r}"
    assert "already registered" not in result.stderr + result.stdout, "no double registration"


def _answer(title: str) -> MediaInfo:
    return MediaInfo(
        source_url="https://youtu.be/x",
        title=title,
        platform="YouTube",
        webpage_url="https://youtu.be/x",
        extension="mp4",
        thumbnail=None,
        duration=10,
        filesize_approx=1,
        is_live=False,
    )


def test_the_metadata_probe_races_the_fast_clients_and_takes_the_first_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The intake budget is "as fast as the menu can appear", so the probe asks
    the two metadata-fast clients (``mweb``, ``tv``) *at the same time* and
    serves the menu from whichever answers first — the wait is the minimum of
    the two, not the sum yt-dlp spends walking a client list one player response
    at a time. The loser is asked anyway (its answer is simply not waited for),
    one single-client request per racer: no request names a client twice."""
    extractor = _extractor(youtube_clients=("mweb", "tv", "web"))
    seen: list[tuple[str, ...]] = []
    mweb, tv = _answer("mweb"), _answer("tv")

    def fake_sync(url: str, *, youtube_clients: tuple[str, ...] | None = None) -> MediaInfo:
        seen.append(tuple(youtube_clients or ()))
        if youtube_clients == ("mweb",):
            time.sleep(0.2)
            return mweb
        return tv

    monkeypatch.setattr(extractor, "_extract_sync", fake_sync)

    info = asyncio.run(extractor.extract("https://youtu.be/x"))

    deadline = time.monotonic() + 2  # the loser thread finishes after the winner returns
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert info is tv, "the first answer wins — the other racer is not waited for"
    assert set(seen) == {("mweb",), ("tv",)}, "one single-client request per racer"


def test_the_failing_race_falls_back_to_the_plain_probe_with_the_whole_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both raced clients failing is not the answer the user gets: the plain
    probe — the configured list, retries and all — is the fallback of last
    resort, and its error is the one that reaches the user exactly as before
    the race existed."""
    extractor = _extractor(youtube_clients=("mweb", "tv", "web"))
    seen: list[tuple[str, ...] | None] = []
    answer = _answer("plain")

    def fake_sync(url: str, *, youtube_clients: tuple[str, ...] | None = None) -> MediaInfo:
        seen.append(tuple(youtube_clients) if youtube_clients is not None else None)
        if youtube_clients is not None:
            raise ExtractionError("GENERAL", "client refused")
        return answer

    monkeypatch.setattr(extractor, "_extract_sync", fake_sync)

    info = asyncio.run(extractor.extract("https://youtu.be/x"))

    assert info is answer, "the plain probe is the fallback of last resort"
    assert set(seen[:2]) == {("mweb",), ("tv",)}, "both racers failed first"
    assert seen[2] is None, "and the fallback names no override — the configured list, whole"


def test_a_client_list_without_the_fast_pair_keeps_the_plain_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured list without both metadata-fast clients must never grow new
    clients behind the operator's back: one probe, with the configured list."""
    extractor = _extractor(youtube_clients=("tv",))
    seen: list[tuple[str, ...] | None] = []

    def fake_sync(url: str, *, youtube_clients: tuple[str, ...] | None = None) -> MediaInfo:
        seen.append(youtube_clients)
        return _answer("plain")

    monkeypatch.setattr(extractor, "_extract_sync", fake_sync)

    asyncio.run(extractor.extract("https://youtu.be/x"))

    assert seen == [None], "one probe, no override — the configured list is the whole story"


def test_the_cookie_authenticated_race_races_the_cookie_compatible_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signed-in probe races the pair the jar can actually use. ``tv`` +
    session cookies is exactly the "the page needs to be reloaded"
    (``SESSION_STALE``) failure, so ``tv`` sits this race out and ``web`` takes
    its leg: the race still fires (two cookie-compatible racers), and no leg
    ever names a cookie-incompatible client."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t2147483647\tLOGIN_INFO\tv\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t2147483647\tSAPISID\tv\n",
        encoding="utf-8",
    )
    extractor = _extractor(
        cookie_file=jar,
        youtube_clients=("android", "ios", "mweb", "tv", "web"),  # the shipped default
    )
    seen: list[tuple[str, ...]] = []
    both_legs_in = threading.Barrier(2, timeout=5)

    def fake_sync(url: str, *, youtube_clients: tuple[str, ...] | None = None) -> MediaInfo:
        seen.append(tuple(youtube_clients or ()))
        try:
            both_legs_in.wait()  # both legs are entered before either answers
        except threading.BrokenBarrierError:
            pass
        return _answer("cookie-compatible")

    monkeypatch.setattr(extractor, "_extract_sync", fake_sync)

    asyncio.run(extractor.extract("https://youtu.be/x"))

    assert set(seen) == {("mweb",), ("web",)}, "the cookie-compatible pair — no tv leg"


def test_the_menu_never_advertises_a_rung_the_chain_would_trade_down() -> None:
    """P0.1's second rule, pinned: the menu and the chain agree, forever.

    A rung is advertised only when the production format chain delivers *that*
    rung: 1440p/2160p that exist only as VP9/AV1 resolve to 1080p H.264 through
    the chain's codec-first steps (MP4 is what Telegram streams inline), so the
    menu must not promise them. Every advertised row is checked against the very
    selector the download uses, and the hidden rows stay hidden for the honest
    reason — the chain trades them down — not to be "fixed" by widening the
    menu.
    """

    def video(fmt_id: str, height: int, vcodec: str, ext: str = "mp4") -> dict[str, Any]:
        return {
            "format_id": fmt_id,
            "url": f"https://example.invalid/{fmt_id}",
            "ext": ext,
            "height": height,
            "width": height * 16 // 9,
            "vcodec": vcodec,
            "acodec": "none",
            "filesize": 1_000_000,
        }

    def audio(fmt_id: str, acodec: str, ext: str = "m4a") -> dict[str, Any]:
        return {
            "format_id": fmt_id,
            "url": f"https://example.invalid/{fmt_id}",
            "ext": ext,
            "acodec": acodec,
            "vcodec": "none",
            "filesize": 500_000,
        }

    info: dict[str, Any] = {
        "formats": [
            audio("140", "mp4a.40.2"),
            audio("251", "opus", "webm"),
            *(video(f"avc{h}", h, "avc1.64002a") for h in (144, 240, 360, 480, 720, 1080)),
            *(video(f"av01_{h}", h, "av01.0.13M.08") for h in (1440, 2160)),
            *(video(f"vp9_{h}", h, "vp9", "webm") for h in (1440, 2160)),
        ]
    }

    rungs = [option.label_p for option in video_options(info)]
    assert rungs == [1080, 720, 480, 360, 240, 144], (
        "the VP9/AV1-only upper rungs are not advertised"
    )

    for option in video_options(info):
        streams = selected_streams(info, option.height)
        picked = next(f for f in streams if f.get("vcodec") not in (None, "none"))
        assert quality_label_p(picked.get("width"), picked.get("height")) == option.label_p, (
            f"the {option.label_p}p row must land on {option.label_p}p"
        )

    for hidden in (2160, 1440):
        streams = selected_streams(info, hidden)
        picked = next(f for f in streams if f.get("vcodec") not in (None, "none"))
        assert int(picked["height"]) < hidden, (
            "hidden exactly because the chain trades the tap down"
        )


# ---------------------------------------------------------------------------
# F1: delivered videos are always an inline-playable container
# ---------------------------------------------------------------------------


def test_merge_output_prefers_mp4_then_webm_and_never_an_unplayable_container() -> None:
    """Telegram plays .mp4/.webm inline; .mkv/.avi/.mov/.wmv arrive silent or as
    files. The merge preference must name a playable container first (mp4, the
    H.264+AAC union) with webm as the fallback — never an unplayable one."""
    parts = [part.strip().lower() for part in MERGE_OUTPUT_FORMAT.split("/")]
    assert parts[0] == "mp4", f"mp4 is the preferred merge container, got {MERGE_OUTPUT_FORMAT!r}"
    assert parts[1] == "webm", f"webm is the fallback merge container, got {MERGE_OUTPUT_FORMAT!r}"
    for forbidden in (".mkv", ".avi", ".mov", ".wmv"):
        assert forbidden.lstrip(".") not in parts, (
            f"{forbidden} is not playable inline in Telegram and must never be a merge target"
        )
