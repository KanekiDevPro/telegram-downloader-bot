"""Application-layer evasion: which clients YouTube sees, and over which IP family.

Two settings, one idea: YouTube's per-client bot checks are not equal, so the bot
stops letting yt-dlp pick the client whose visitor binding produces the
"the page needs to be reloaded" (``SESSION_STALE``) failure — and it stops offering
IPv6 addresses at all, because the tunnel's IPv6 ranges are the ones that get flagged.

The interesting part is *which* clients, and that is not folklore here: the installed
yt-dlp carries a GVS PO-token policy per client, and the clients a "spoof a phone"
list usually names (``android``, ``ios``) are the ones whose policy says a token is
required. The tests below read that table through the same helper the doctor uses, so
a yt-dlp upgrade that moves a client out of the token-free group fails here instead of
quietly costing every download its extraction.

Since production telemetry showed web-served downloads crawling at throttle speed
on the WARP address (50.4 s for a job whose upload streamed out in 9.9 s), the
order is also a *bandwidth* decision: the clients phones and TVs stream with come
first, the PO-token provider pays their token cost, ``tv`` is the token-free
lifeline, and ``web`` is last.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import DEFAULT_YOUTUBE_CLIENTS, Settings
from services import doctor as doctor_service
from services.doctor import CLIENTS_CHECK_NAME, _clients_check
from services.extractor import (
    IPV4_ANY,
    ExtractorService,
    effective_youtube_clients,
    youtube_client_facts,
)

#: The clients the old "spoof a device and skip the token" advice names. ``tv`` is in
#: the token-free group in this yt-dlp, the other two are not — which is the whole
#: finding: the advice is half right for the wrong reason. (The default list now
#: names them too — with the PO-token provider paying the token cost, not as a
#: bypass; see ``DEFAULT_YOUTUBE_CLIENTS``.)
PHONE_CLIENTS = ("android", "ios")


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _extractor(tmp_path: Path, **kwargs: object) -> ExtractorService:
    return ExtractorService(tmp_path, js_runtime="none", **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------


def test_the_default_client_list_puts_the_streaming_clients_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order is a bandwidth decision, measured in production: web-family
    URLs are throttled on datacenter/WARP addresses (50.4 s download for the
    same file that streamed to Telegram in 9.9 s), while the clients phones and
    TVs stream with serve the same ladder at full speed. The token policy is the
    other half: ``android``/``ios``/``mweb``/``web`` *need* GVS PO tokens in this
    yt-dlp and the bgutil provider supplies them, ``tv`` is the one token-free
    client left standing (the lifeline when the provider is down), and ``web``
    is last — it is the throttled one *and* the client whose visitor binding
    produces the SESSION_STALE failure.
    """
    settings = _settings(monkeypatch)

    assert tuple(settings.ytdlp_youtube_clients) == DEFAULT_YOUTUBE_CLIENTS
    unknown, required, free = youtube_client_facts(settings.ytdlp_youtube_clients)
    assert unknown == (), "the shipped default must be valid in the installed yt-dlp"
    assert DEFAULT_YOUTUBE_CLIENTS[:3] == ("android", "ios", "mweb"), "streaming clients first"
    assert DEFAULT_YOUTUBE_CLIENTS[-1] == "web", "web last: the throttled ladder backstop"
    assert set(required) == {"android", "ios", "mweb", "web"}, "the provider pays their token cost"
    assert free == ("tv",), "tv is the token-free lifeline"
    assert settings.ytdlp_force_ipv4 is True


def test_clients_are_accepted_the_way_a_list_is_usually_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for raw, expected in (
        ("tv,visionos", ["tv", "visionos"]),
        ("tv visionos", ["tv", "visionos"]),
        ("TV,VisionOS", ["tv", "visionos"]),
        ('["tv", "visionos"]', ["tv", "visionos"]),
        ("tv,", ["tv"]),
    ):
        settings = _settings(monkeypatch, YTDLP_YOUTUBE_CLIENTS=raw)
        assert settings.ytdlp_youtube_clients == expected, raw


def test_an_empty_client_list_means_let_ytdlp_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blank is a choice — *not* the default — so the app can step out of the way."""
    settings = _settings(monkeypatch, YTDLP_YOUTUBE_CLIENTS="")

    assert settings.ytdlp_youtube_clients == []


def test_the_ipv4_flag_accepts_the_usual_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for raw, expected in (("0", False), ("no", False), ("off", False), ("", False), ("1", True)):
        settings = _settings(monkeypatch, YTDLP_FORCE_IPV4=raw)
        assert settings.ytdlp_force_ipv4 is expected, raw


# ---------------------------------------------------------------------------
# What yt-dlp is actually handed
# ---------------------------------------------------------------------------


def test_the_clients_reach_yt_dlp_under_the_key_it_reads(tmp_path: Path) -> None:
    """``player_client`` is the name yt-dlp reads; ``client`` is read by nobody."""
    extractor = _extractor(tmp_path, youtube_clients=("tv", "visionos"))

    args = extractor._base_opts(extract_only=True)["extractor_args"]

    assert args == {"youtube": {"player_client": ["tv", "visionos"], "formats": ["dashy"]}}


def test_the_provider_and_the_clients_share_the_extractor_args_option(tmp_path: Path) -> None:
    """Two settings, one yt-dlp option: an assignment that dropped either is a bug."""
    extractor = _extractor(
        tmp_path,
        youtube_clients=("tv",),
        pot_provider_url="http://pot-provider:4416/",
    )

    args = extractor._base_opts(extract_only=True)["extractor_args"]

    assert args == {
        "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]},
        "youtube": {"player_client": ["tv"], "formats": ["dashy"]},
    }


def test_a_client_list_is_never_sent_without_a_reason_to_send_it(tmp_path: Path) -> None:
    """Identity stays neutral (a probe/test does not inherit a spoof), while the
    download mechanics every request wants ride along regardless."""
    extractor = _extractor(tmp_path)

    assert extractor.extractor_args["youtube"] == {"formats": ["dashy"]}
    assert "player_client" not in extractor._base_opts(extract_only=True)["extractor_args"]["youtube"]


def test_clients_are_lower_cased_and_not_repeated(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path, youtube_clients=("TV", "tv", " Visionos ", ""))

    assert extractor.youtube_clients == ("tv", "visionos")


def test_ipv4_only_is_yt_dlps_own_force_ipv4(tmp_path: Path) -> None:
    """``source_address = 0.0.0.0`` is exactly what ``--force-ipv4`` sets."""
    on = _extractor(tmp_path, force_ipv4=True)
    off = _extractor(tmp_path, force_ipv4=False)

    assert on._base_opts(extract_only=True)["source_address"] == IPV4_ANY
    assert IPV4_ANY == "0.0.0.0"
    assert "source_address" not in off._base_opts(extract_only=True)


def test_the_proxy_and_the_cookies_are_untouched_by_the_evasion(tmp_path: Path) -> None:
    """The two options are additive: the tunnel and the jar are not disturbed."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t2147483647\tLOGIN_INFO\tv\n",
        encoding="utf-8",
    )
    extractor = _extractor(
        tmp_path,
        proxy="socks5://user:pw@127.0.0.1:1080",
        cookie_file=jar,
        youtube_clients=("tv",),
        force_ipv4=True,
    )

    opts = extractor._base_opts(extract_only=True)

    assert opts["proxy"] == "socks5://user:pw@127.0.0.1:1080"
    assert opts["extractor_args"]["youtube"]["player_client"] == ["tv"]
    assert opts["source_address"] == IPV4_ANY
    assert opts["cookiefile"] != str(jar), "the jar is still handed over as a writable copy"


def test_a_client_name_from_the_old_device_spoofing_advice_is_half_wrong(
    tmp_path: Path,
) -> None:
    """The measured claim, pinned: phones want a token in this yt-dlp, ``tv`` does not."""
    unknown, required, free = youtube_client_facts(PHONE_CLIENTS + ("tv",))

    assert unknown == ()
    assert set(required) == set(PHONE_CLIENTS)
    assert free == ("tv",)


def test_a_name_yt_dlp_does_not_know_is_reported_not_applied(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extractor = _extractor(tmp_path, youtube_clients=("tv", "not_a_client"))

    unknown, _required, free = youtube_client_facts(extractor.youtube_clients)

    assert unknown == ("not_a_client",)
    assert free == ("tv",)
    # yt-dlp skips it silently; the operator hears about it once, at startup.
    assert "not_a_client" in caplog.text


def test_a_yt_dlp_without_that_table_says_so_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def failing(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("yt_dlp.extractor.youtube"):
            raise ImportError("moved")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", failing)

    assert youtube_client_facts(("tv",)) == ((), (), ())


# ---------------------------------------------------------------------------
# What the admin is told
# ---------------------------------------------------------------------------


def test_the_row_names_the_clients_and_the_family(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path, youtube_clients=("tv", "visionos"), force_ipv4=True)

    check = _clients_check(extractor)

    assert check.name == CLIENTS_CHECK_NAME
    assert check.status == "ok"
    assert "tv" in check.detail and "0.0.0.0" in check.detail


def test_the_row_warns_about_a_client_that_needs_a_token(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path, youtube_clients=("android", "tv"))

    check = _clients_check(extractor)

    assert check.status == "warn"
    assert "android" in check.detail and "PO token" in check.detail


def test_the_token_warning_stands_down_when_the_provider_pays(
    tmp_path: Path,
) -> None:
    """A token-required client is only a problem without a token source: with
    the bgutil provider configured the row stays ok and still names the
    clients, so the fact is visible without the alarm."""
    extractor = _extractor(
        tmp_path, youtube_clients=("android", "tv"), pot_provider_url="http://pot-provider:4416"
    )

    check = _clients_check(extractor)

    assert check.status == "ok"
    assert "android" in check.detail and "PO token" in check.detail
    assert "provider" in check.detail


def test_the_row_warns_about_a_client_yt_dlp_would_skip(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path, youtube_clients=("tv", "iosx"))

    check = _clients_check(extractor)

    assert check.status == "warn"
    assert "iosx" in check.detail


def test_the_row_says_yt_dlp_decides_when_the_list_is_empty(tmp_path: Path) -> None:
    extractor = _extractor(tmp_path, force_ipv4=False)

    check = _clients_check(extractor)

    assert check.status == "ok"
    assert "yt-dlp" in check.detail and "IPv4/IPv6" in check.detail


def test_the_row_sits_with_the_rest_of_the_engine_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch)

    checks = doctor_service._base_checks(
        settings, _extractor(tmp_path, youtube_clients=("tv",)), None, None, None
    )

    names = [check.name for check in checks]
    assert names.index(CLIENTS_CHECK_NAME) == names.index("JS runtime") + 1


# ---------------------------------------------------------------------------
# The cookie-aware hierarchy
# ---------------------------------------------------------------------------

VALID_JAR = (
    "# Netscape HTTP Cookie File\n"
    "#HttpOnly_.youtube.com\tTRUE\t/\tFALSE\t2147483647\tLOGIN_INFO\tv\n"
)


def _jar(tmp_path: Path) -> Path:
    jar = tmp_path / "cookies.txt"
    jar.write_text(VALID_JAR, encoding="utf-8")
    return jar


def test_a_cookie_blind_client_costs_a_round_trip_only_anonymous(tmp_path: Path) -> None:
    """With a signed-in jar, ``visionos`` is a wasted player request.

    ``visionos`` is the one client in the default list that cannot carry a
    cookie session (yt-dlp's own JS-less anonymous fallback): when cookies are
    active its player response is either the anonymous one or one yt-dlp
    *skips* outright (``_video.py``: "got player responses for video … instead
    of …"), and either way a whole round-trip bought nothing. So the jar makes
    the request list shorter — and the same list stays whole without one.
    """
    clients = ("visionos", "web_embedded", "tv_downgraded", "web")

    with_jar = _extractor(tmp_path, cookie_file=_jar(tmp_path), youtube_clients=clients)
    without = _extractor(tmp_path, cookie_file=None, youtube_clients=clients)

    assert with_jar._base_opts(extract_only=True)["extractor_args"]["youtube"]["player_client"] == [
        "web_embedded",
        "tv_downgraded",
        "web",
    ]
    assert without._base_opts(extract_only=True)["extractor_args"]["youtube"]["player_client"] == (
        list(clients)
    )


def test_browser_cookies_count_as_cookies_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extractor = _extractor(
        tmp_path, cookies_from_browser="chrome", youtube_clients=("visionos", "tv")
    )
    monkeypatch.setattr("services.extractor.browser_cookie_jar_is_usable", lambda spec: True)
    extractor._browser_cookies_ok = None

    args = extractor._base_opts(extract_only=True)["extractor_args"]

    assert args["youtube"]["player_client"] == ["tv"]


def test_the_configured_list_is_never_rewritten_behind_the_operator_s_back(
    tmp_path: Path,
) -> None:
    """``extractor_args`` reports what was configured; only the request adapts."""
    clients = ("visionos", "web_embedded")
    extractor = _extractor(tmp_path, cookie_file=_jar(tmp_path), youtube_clients=clients)

    assert extractor.extractor_args["youtube"]["player_client"] == list(clients)


def test_the_jar_less_second_opinion_keeps_every_client(tmp_path: Path) -> None:
    """``allow_cookies=False`` is the anonymous retry — there ``visionos`` fits."""
    extractor = _extractor(
        tmp_path, cookie_file=_jar(tmp_path), youtube_clients=("visionos", "tv")
    )

    args = extractor._base_opts(extract_only=True, allow_cookies=False)["extractor_args"]

    assert args["youtube"]["player_client"] == ["visionos", "tv"]


def test_the_filter_is_a_pure_function_of_the_cookie_state() -> None:
    assert effective_youtube_clients(("visionos", "tv"), cookies_active=True) == ("tv",)
    assert effective_youtube_clients(("visionos", "tv"), cookies_active=False) == ("visionos", "tv")
    assert effective_youtube_clients((), cookies_active=True) == ()
    # Variant spellings (``base.variant``) are judged by their base client.
    assert effective_youtube_clients(("visionos.tv",), cookies_active=True) == ()
    assert effective_youtube_clients(("web_embedded.visionos",), cookies_active=True) == (
        "web_embedded.visionos",
    )
