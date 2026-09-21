"""YouTube doctor tests (offline).

The doctor exists because every YouTube failure looks the same from the outside.
These tests drive each branch of the decision ladder with a stubbed probe, so the
advice it hands an admin cannot silently drift away from the actual cause.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import Settings, probe_url
from services import doctor as doctor_service
from services.doctor import Check, run_youtube_doctor
from services.extractor import (
    CookieJarState,
    ExtractionError,
    ExtractorService,
    MediaInfo,
    MountInfo,
)

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str, *, http_only: bool = True) -> str:
    prefix = "#HttpOnly_" if http_only else ""
    return f"{prefix}.youtube.com\tTRUE\t/\tFALSE\t2147483647\t{name}\tv\n"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _extractor(
    tmp_path: Path, jar: Path | None, **kwargs: str
) -> ExtractorService:
    # "none" keeps whatever happens to be installed on the test host out of the assertions.
    return ExtractorService(tmp_path, cookie_file=jar, js_runtime="none", **kwargs)  # type: ignore[arg-type]


async def _reachable(url: str, timeout: float = 5.0) -> bool:
    return True


async def _unreachable(url: str, timeout: float = 5.0) -> bool:
    return False


#: The two helper probes (PO-token provider, YouTube session server) are stubbed
#: healthy for the whole suite in ``tests/conftest.py`` — a report here is about the
#: code, not about which containers this machine happens to be running. Individual
#: tests patch over that when a helper's state is the thing under test.


def _probe(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    """Patch the metadata probe with either a MediaInfo or an exception."""

    async def fake_extract(_self: ExtractorService, url: str) -> MediaInfo:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]

    monkeypatch.setattr(ExtractorService, "extract", fake_extract)


def _info() -> MediaInfo:
    return MediaInfo(
        source_url="https://youtu.be/x",
        title="Big Buck Bunny",
        platform="youtube",
        webpage_url="https://youtu.be/x",
        extension="mp4",
        thumbnail=None,
        duration=596,
        filesize_approx=None,
        is_live=False,
    )


BLOCKED = ExtractionError("EXTRACTOR_BLOCKED", "سایت مبدأ دانلود را مسدود کرد…")


def _statuses(report: doctor_service.DoctorReport) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

async def test_working_path_reports_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(monkeypatch, COOKIE_FILE=str(jar))
    extractor = _extractor(tmp_path, jar)
    _probe(monkeypatch, _info())

    report = await run_youtube_doctor(settings, extractor)

    assert report.healthy is True
    assert _statuses(report)["کوکی"] == "ok"
    assert _statuses(report)["منبع کوکی"] == "ok"
    assert _statuses(report)["تست زنده"] == "ok"
    assert "سالم" in report.verdict


# ---------------------------------------------------------------------------
# Where the jar comes from: mount, export time, and whether it is the one in use
# ---------------------------------------------------------------------------

def _state(**overrides: object) -> CookieJarState:
    base: dict[str, object] = {
        "path": Path("/cookies/cookies.txt"),
        "kind": "ok",
        "size_bytes": 4096,
        "exported_at": 1_000_000.0,
        "mount": None,
        "writable": True,
        "cookie_count": 31,
        "missing_login": (),
        "copy_path": Path("/app/downloads/.cookies/cookies.txt"),
        "copy_refreshed_at": 1_000_060.0,
        "in_sync": True,
    }
    base.update(overrides)
    return CookieJarState(**base)  # type: ignore[arg-type]


def _storage_check(extractor: ExtractorService) -> Check:
    return doctor_service._cookie_storage_check(extractor)


def test_a_read_only_mount_is_reported_with_its_host_path() -> None:
    state = _state(
        mount=MountInfo(
            point="/cookies",
            root="/Users/mo/bot",
            filesystem="9p",
            source="C:\\",
            read_only=True,
        ),
        writable=False,
    )

    wording = doctor_service.mount_wording(state)

    assert "mount /cookies" in wording
    assert "9p" in wording
    assert "فقط-خواندنی" in wording
    assert "/Users/mo/bot" in wording, "the host directory is what an operator edits"


def test_a_host_file_is_described_without_a_mount() -> None:
    assert "بدون mount" in doctor_service.mount_wording(_state())


def test_storage_check_names_a_missing_jar_and_a_placeholder(tmp_path: Path) -> None:
    missing = _extractor(tmp_path, tmp_path / "cookies.txt")
    assert _storage_check(missing).status == "warn"
    assert "فایل نیست" in _storage_check(missing).detail

    (tmp_path / "cookies.txt").mkdir()
    placeholder = _extractor(tmp_path, tmp_path / "cookies.txt")
    assert _storage_check(placeholder).status == "fail"
    assert "پوشه" in _storage_check(placeholder).detail

    none_configured = _extractor(tmp_path, None)
    assert _storage_check(none_configured).status == "warn"


def test_a_fresh_export_that_is_not_in_use_yet_is_flagged_not_failed(
    tmp_path: Path,
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    extractor = _extractor(tmp_path, jar)
    extractor._base_opts(extract_only=True)  # yt-dlp gets its copy
    jar.write_text(HEADER + _row("LOGIN_INFO", http_only=False), encoding="utf-8")

    check = _storage_check(extractor)

    assert check.status == "warn", "waiting for the next download is not a failure"
    assert "دانلود بعدی" in check.detail
    assert "کوکی" in check.detail and str(jar) in check.detail


def test_the_storage_check_says_when_the_export_is_the_one_in_use(tmp_path: Path) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    extractor = _extractor(tmp_path, jar)
    extractor._base_opts(extract_only=True)

    check = _storage_check(extractor)

    assert check.status == "ok"
    assert "استفاده می‌شود" in check.detail
    assert "2 کوکی" in check.detail


async def test_offline_mode_skips_the_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, COOKIE_FILE=str(tmp_path / "absent.txt"))
    extractor = _extractor(tmp_path, tmp_path / "absent.txt")

    report = await run_youtube_doctor(settings, extractor, probe=False)

    assert "تست زنده" not in _statuses(report)
    assert "انجام نشد" in report.verdict


# ---------------------------------------------------------------------------
# The failure ladder: each cause must produce its own next step
# ---------------------------------------------------------------------------

async def test_jar_without_a_login_blames_the_cookies_not_the_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("__Secure-3PSID") + _row("__Secure-3PAPISID"), encoding="utf-8")
    settings = _settings(monkeypatch, COOKIE_FILE=str(jar))
    extractor = _extractor(tmp_path, jar)
    _probe(monkeypatch, BLOCKED)

    report = await run_youtube_doctor(settings, extractor)

    assert report.healthy is False
    assert _statuses(report)["کوکی"] == "warn"
    assert "LOGIN_INFO" in dict((c.name, c.detail) for c in report.checks)["کوکی"]
    assert "export_cookies.py" in report.next_step
    assert "YTDLP_PROXY" not in report.next_step  # the tempting wrong fix


async def test_complete_login_without_a_provider_suggests_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(monkeypatch, COOKIE_FILE=str(jar), YTDLP_POT_PROVIDER_URL="")
    extractor = _extractor(tmp_path, jar)
    _probe(monkeypatch, BLOCKED)

    report = await run_youtube_doctor(settings, extractor)

    assert "pot-provider" in report.next_step
    assert "YTDLP_POT_PROVIDER_URL" in report.next_step


async def test_configured_but_dead_provider_is_called_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(
        monkeypatch, COOKIE_FILE=str(jar), YTDLP_POT_PROVIDER_URL="http://pot-provider:4416"
    )
    extractor = _extractor(tmp_path, jar, pot_provider_url="http://pot-provider:4416")
    _probe(monkeypatch, BLOCKED)

    async def _provider_down(url: str, timeout: float = 5.0) -> doctor_service.PotProvider:
        return doctor_service.PotProvider(reachable=False, error="پاسخ نمی‌دهد")

    monkeypatch.setattr(doctor_service, "probe_pot_provider", _provider_down)

    report = await run_youtube_doctor(settings, extractor)

    assert _statuses(report)["PO token"] == "fail"
    assert "در دسترس نیست" in report.verdict


async def test_everything_healthy_but_still_blocked_means_the_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(
        monkeypatch,
        COOKIE_FILE=str(jar),
        YTDLP_POT_PROVIDER_URL="http://pot-provider:4416",
        YTDLP_PROXY="socks5://127.0.0.1:1080",
    )
    extractor = _extractor(tmp_path, jar, pot_provider_url="http://pot-provider:4416")
    _probe(monkeypatch, BLOCKED)

    report = await run_youtube_doctor(settings, extractor)

    assert _statuses(report)["PO token"] == "ok"
    assert "پروکسی را عوض کنید" in report.next_step


STALE = ExtractionError("SESSION_STALE", "یوتیوب این درخواست را نپذیرفت…")


async def test_stale_session_points_at_the_provider_before_a_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YouTube accepted the login but refused the request — cheapest fix first."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(monkeypatch, COOKIE_FILE=str(jar), YTDLP_POT_PROVIDER_URL="")
    extractor = _extractor(tmp_path, jar)
    _probe(monkeypatch, STALE)

    report = await run_youtube_doctor(settings, extractor)

    assert "pot-provider" in report.next_step
    assert "YTDLP_PROXY" not in report.next_step


async def test_stale_session_with_everything_ready_reports_the_built_in_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(
        monkeypatch,
        COOKIE_FILE=str(jar),
        YTDLP_POT_PROVIDER_URL="http://pot-provider:4416",
        EXTRACTOR_RETRY_ATTEMPTS="2",
    )
    extractor = _extractor(tmp_path, jar, pot_provider_url="http://pot-provider:4416")
    _probe(monkeypatch, STALE)

    report = await run_youtube_doctor(settings, extractor)

    # The advice must match what the bot does, not what it used to do.
    assert "2 بار دیگر" in report.next_step
    assert "YTDLP_PROXY" in report.next_step


async def test_stale_session_advice_drops_the_retry_note_when_retrying_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("LOGIN_INFO") + _row("SAPISID"), encoding="utf-8")
    settings = _settings(
        monkeypatch,
        COOKIE_FILE=str(jar),
        YTDLP_POT_PROVIDER_URL="http://pot-provider:4416",
        EXTRACTOR_RETRY_ATTEMPTS="0",
    )
    extractor = _extractor(tmp_path, jar, pot_provider_url="http://pot-provider:4416")
    _probe(monkeypatch, STALE)
    monkeypatch.setattr(doctor_service, "http_reachable", _reachable)

    report = await run_youtube_doctor(settings, extractor)

    assert "بار دیگر" not in report.next_step
    assert "تلاش دوباره" in report.next_step


async def test_stale_session_is_not_confused_with_a_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block means the login is wrong; a stale session means only the request is."""
    jar = tmp_path / "cookies.txt"
    jar.write_text(HEADER + _row("__Secure-3PSID"), encoding="utf-8")
    settings = _settings(monkeypatch, COOKIE_FILE=str(jar), YTDLP_POT_PROVIDER_URL="")
    extractor = _extractor(tmp_path, jar)
    _probe(monkeypatch, STALE)

    report = await run_youtube_doctor(settings, extractor)

    assert "ناشناس" not in report.verdict
    assert "provider" in report.next_step


async def test_non_youtube_failure_reports_the_real_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COOKIE_FILE=str(tmp_path / "absent.txt"))
    extractor = _extractor(tmp_path, None)
    _probe(monkeypatch, ExtractionError("PRIVATE_VIDEO", "ویدیو در دسترس نیست."))

    report = await run_youtube_doctor(settings, extractor)

    assert "PRIVATE_VIDEO" in report.verdict
    assert report.next_step == "ویدیو در دسترس نیست."


async def test_unexpected_probe_crash_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COOKIE_FILE=str(tmp_path / "absent.txt"))
    extractor = _extractor(tmp_path, None)
    _probe(monkeypatch, RuntimeError("boom"))

    report = await run_youtube_doctor(settings, extractor)

    assert report.healthy is False
    assert "GENERAL" in report.verdict
    assert "RuntimeError: boom" in report.next_step


# ---------------------------------------------------------------------------
# Missing pieces are surfaced even when nothing is blocking
# ---------------------------------------------------------------------------

async def test_missing_jar_and_runtime_are_warned_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COOKIE_FILE="")
    extractor = _extractor(tmp_path, None)
    _probe(monkeypatch, _info())

    report = await run_youtube_doctor(settings, extractor)

    statuses = _statuses(report)
    assert statuses["کوکی"] == "warn"
    assert statuses["JS runtime"] == "warn"
    assert "JS runtime" in report.next_step


def test_render_lists_every_check() -> None:
    report = doctor_service.DoctorReport(
        checks=(Check("ffmpeg", "ok", "موجود"), Check("کوکی", "warn", "لاگین ندارد")),
        verdict="⛔️ کار نمیکند",
        next_step="کوکی را دوباره بگیرید",
    )

    text = report.render()

    assert "✅ ffmpeg: موجود" in text
    assert "⚠️ کوکی: لاگین ندارد" in text
    assert "قدم بعدی: کوکی را دوباره بگیرید" in text


# ---------------------------------------------------------------------------
# Startup: a configured-but-dead provider must not poison every download
# ---------------------------------------------------------------------------

async def test_startup_keeps_a_provider_that_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as entrypoint

    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL="http://pot-provider:4416")
    monkeypatch.setattr(entrypoint, "http_reachable", _reachable)

    # The engine gets the address *this process* can call — the service name in the
    # compose network, the published loopback port on a host.
    assert await entrypoint.resolve_pot_provider(settings) == probe_url(
        "http://pot-provider:4416"
    )


async def test_startup_drops_a_provider_that_is_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The bgutil plugin fails every download while its provider is unreachable."""
    import main as entrypoint

    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL="http://pot-provider:4416")
    monkeypatch.setattr(entrypoint, "http_reachable", _unreachable)
    monkeypatch.setattr(entrypoint, "POT_PROVIDER_PROBE_ATTEMPTS", 1)

    with caplog.at_level("WARNING"):
        resolved = await entrypoint.resolve_pot_provider(settings)

    assert resolved == ""
    assert "does not answer" in " ".join(r.getMessage() for r in caplog.records)


async def test_startup_waits_for_a_provider_that_is_still_coming_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `docker compose up -d` starts both: a node server a moment behind is normal."""
    import main as entrypoint

    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL="http://pot-provider:4416")
    answers = iter([False, True])

    async def _second_time(url: str, timeout: float = 5.0) -> bool:
        return next(answers)

    monkeypatch.setattr(entrypoint, "http_reachable", _second_time)
    monkeypatch.setattr(entrypoint, "POT_PROVIDER_PROBE_DELAY_S", 0)

    assert await entrypoint.resolve_pot_provider(settings) == probe_url(
        "http://pot-provider:4416"
    )


async def test_startup_does_not_probe_when_no_provider_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as entrypoint

    settings = _settings(monkeypatch, YTDLP_POT_PROVIDER_URL="")
    monkeypatch.setattr(entrypoint, "http_reachable", _unreachable)

    assert await entrypoint.resolve_pot_provider(settings) == ""


def test_probe_url_constant_is_a_youtube_link() -> None:
    assert doctor_service.DEFAULT_PROBE_URL.startswith("https://www.youtube.com/watch?v=")
