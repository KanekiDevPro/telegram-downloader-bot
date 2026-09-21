"""The fallback section of the doctor report.

An admin reads ``/doctor`` to learn what stands between a blocked link and the
user's file. Since the fallback engine is exactly that stand-in, its line has to
answer three things without a log hunt: *which* instance, in *what* state, speaking
*which* dialect — and the four states have to stay distinguishable, because
"configured" (🟢), "left alone after a failure" (🟡), "wants a key" (🔴) and "not
reachable" (❌) each call for a different action.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from services import doctor as doctor_service
from services.cobalt import CobaltError, CobaltMedia
from services.cobalt_cookies import CobaltCookieState
from services.doctor import (
    COBALT_RESTART_FIX,
    FallbackHealth,
    cobalt_cookie_check,
    run_youtube_doctor,
)
from services.extractor import ExtractionError, ExtractorService, MediaInfo

URL = "https://api.cobalt.example"
BLOCKED = ExtractionError("EXTRACTOR_BLOCKED", "سایت مبدأ دانلود را مسدود کرد…")


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _extractor(tmp_path: Path) -> ExtractorService:
    return ExtractorService(tmp_path, cookie_file=None, js_runtime="none")


def _probe(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
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


class FakeCobalt:
    """A client that answers (or fails) without a network, like the worker's."""

    def __init__(
        self,
        *,
        media: CobaltMedia | None = None,
        error: CobaltError | None = None,
        dialect: str | None = "v10",
        quarantined: bool = False,
        reason: str = "",
        base_url: str = URL,
        started_at: float | None = None,
    ) -> None:
        self.enabled = bool(base_url)
        self.base_url = base_url
        self.dialect = dialect
        self.quarantined = quarantined
        self.quarantine_reason = reason
        self.started_at = started_at
        self._media = media or CobaltMedia(url="https://cdn.example/v.mp4")
        self._error = error
        self.probes = 0

    def node_states(self) -> tuple[object, ...]:
        """No pool in this stub: the tests here are about the one verdict line."""
        return ()

    async def server_start_time(self) -> float | None:
        """What the report compares the cookie file's stamp against."""
        return self.started_at

    @property
    def available(self) -> bool:
        return self.enabled and not self.quarantined

    async def resolve(self, url: str, media_format: str) -> CobaltMedia:
        self.probes += 1
        if self._error is not None:
            raise self._error
        return self._media

    async def close(self) -> None:
        return None


def _section(report: doctor_service.DoctorReport) -> str:
    """The fallback section as printed: its line, the reason, and the next step.

    The reason and the fix are deliberately on their own lines (a phone screen is
    narrow), so the assertions have to read the whole section — not just its head.
    """
    lines = report.render().splitlines()
    start = next(
        index for index, line in enumerate(lines) if doctor_service.FALLBACK_CHECK_NAME in line
    )
    rest = lines[start + 1 :]
    taken = [line for line in rest if not line.startswith("حکم: ")]
    return "\n".join([lines[start], *taken])


def _line(report: doctor_service.DoctorReport) -> str:
    """The head of that section: the icon, the URL, the state and the dialect."""
    return _section(report).splitlines()[0]


def _cobalt(**kwargs: Any) -> Any:
    """Typed as ``Any``: the fake stands in for the real client in these tests."""
    return FakeCobalt(**kwargs)


# ---------------------------------------------------------------------------
# The four states (+ off) as the admin sees them
# ---------------------------------------------------------------------------


async def test_a_working_instance_is_reported_ready_with_its_dialect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt())

    line = _line(report)
    assert line.startswith("🟢 ")
    assert "آماده به کار" in line
    assert URL in line, "the operator has to know which instance answered"
    assert "v10" in line, "and which dialect it spoke"


async def test_an_instance_that_wants_a_key_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one that matters today: the public instance answers only with a key."""
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, BLOCKED)
    error = CobaltError(
        "ERROR",
        "نمونهٔ کوبالت خطا داد (400): این نمونه احراز هویت می‌خواهد",
        instance=True,
        needs_auth=True,
    )

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt(error=error))

    assert _line(report).startswith("🔴 ")
    assert "نیازمند کلید احراز هویت" in _line(report)
    assert "COBALT_API_KEY" in _section(report), "the fix is named, not just the problem"


async def test_an_unreachable_instance_is_not_confused_with_a_key_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())
    error = CobaltError("UNREACHABLE", "اتصال به نمونهٔ کوبالت برقرار نشد", instance=True)

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt(error=error))

    assert _line(report).startswith("❌ ")
    assert "در دسترس نیست" in _line(report)


async def test_a_quarantined_instance_shows_the_reason_it_was_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """\"Left alone\" is a state, not a silence — and the reason is the diagnosis."""
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())

    report = await run_youtube_doctor(
        settings,
        _extractor(tmp_path),
        cobalt=_cobalt(quarantined=True, reason="ERROR: rate limited"),
        probe=False,
    )

    assert _line(report).startswith("🟡 ")
    assert "قرنطینه" in _line(report)
    assert "rate limited" in _section(report), "and why it is being left alone"


async def test_an_instance_without_a_youtube_session_says_which_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state an embedded instance meets first, and the one a generic "degraded"
    line would hide: the instance is fine (tiktok resolves), YouTube alone needs a
    session — so the fix is cobalt's cookies, *not* the bot's cookie jar."""
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, BLOCKED)
    error = CobaltError(
        "ERROR",
        "کوبالت نتوانست این لینک را بگیرد: … (error.api.youtube.login)",
        upstream="error.api.youtube.login",
    )

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt(error=error))

    assert _line(report).startswith("🔴 ")
    assert "برای یوتیوب سشن/کوکی ندارد" in _line(report)
    assert "cookies.json" in _section(report)
    assert "YOUTUBE_SESSION_SERVER" in _section(report)
    # The primary path's fix is a different thing, and the verdict must not blur
    # the two: the note is about YouTube only, and says so.
    assert "یوتیوب" in report.next_step
    assert "بقیهٔ سایت‌ها" in report.next_step


async def test_an_instance_that_answered_but_refused_the_link_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())
    error = CobaltError("NO_MEDIA", "کوبالت این لینک را آلبوم/لیست دید")  # not instance-level

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt(error=error))

    assert _line(report).startswith("🟡 ")
    assert "این لینک را نگرفت" in _line(report)


async def test_a_disabled_fallback_is_off_not_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty COBALT_API_URL is a configuration, not a fault to fix."""
    # COOKIE_FILE is cleared too: this test is about the fallback line, and the
    # cookie checks have plenty of tests of their own.
    settings = _settings(monkeypatch, COBALT_API_URL="", COOKIE_FILE="")
    _probe(monkeypatch, _info())
    fake = _cobalt(base_url="")  # what main.py builds from an empty setting

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=fake)

    line = _line(report)
    assert line.startswith("⚫️ ") and "خاموش" in line
    assert fake.probes == 0, "nothing is probed when there is nothing configured"
    assert report.healthy is True, "and the download path is still judged on its own"


async def test_an_offline_run_says_so_instead_of_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL)

    report = await run_youtube_doctor(
        settings, _extractor(tmp_path), cobalt=_cobalt(), probe=False
    )

    assert _line(report).startswith("❔ ")
    assert "v10" in _line(report), "what is known without a request is still shown"


async def test_a_caller_without_a_client_gets_no_fabricated_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI runs pass one (see scripts/youtube_doctor.py); a test that does not
    must still produce an honest line rather than a fake 🟢."""
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())

    report = await run_youtube_doctor(settings, _extractor(tmp_path))

    assert _line(report).startswith("❔ ")
    assert "کلاینت fallback" in _section(report)


# ---------------------------------------------------------------------------
# What the fallback changes about the verdict
# ---------------------------------------------------------------------------


async def test_a_blocked_primary_path_says_users_are_still_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL, COOKIE_FILE=str(tmp_path / "none"))
    _probe(monkeypatch, BLOCKED)

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt())

    assert "موتور جایگزین" in report.next_step
    assert "کاربران بلاک" in report.next_step
    assert "کوکی" in report.next_step, "the real fix is still named first"


async def test_a_blocked_path_with_a_broken_net_says_users_are_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, BLOCKED)
    error = CobaltError("ERROR", "auth", instance=True, needs_auth=True)

    report = await run_youtube_doctor(
        settings, _extractor(tmp_path), cobalt=_cobalt(error=error)
    )

    assert "موتور جایگزین هم آماده نیست" in report.next_step


async def test_a_healthy_path_is_not_cluttered_with_fallback_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())

    report = await run_youtube_doctor(settings, _extractor(tmp_path), cobalt=_cobalt())

    assert "موتور جایگزین" not in report.next_step


# ---------------------------------------------------------------------------
# The section, and remembering the verdict across runs
# ---------------------------------------------------------------------------


def test_the_fallback_is_its_own_section() -> None:
    report = doctor_service.DoctorReport(
        checks=(
            doctor_service.Check("ffmpeg", "ok", "موجود"),
            FallbackHealth("ready", URL, "v10", seconds=1.2).as_check(),
        ),
        verdict="v",
        next_step="n",
    )

    lines = report.render().splitlines()
    assert "" in lines, "a blank line separates the fallback from the yt-dlp chain"
    assert lines.index("") == 3, "right after the last yt-dlp check"


class _State:
    """A ``bot_state`` row in memory, written through ``core.database``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.value: str | None = None

        async def set_state(pool: object, key: str, value: str) -> None:
            self.value = value

        async def get_state(pool: object, key: str) -> str | None:
            return self.value

        monkeypatch.setattr(doctor_service.database, "set_state", set_state)
        monkeypatch.setattr(doctor_service.database, "get_state", get_state)


async def test_the_verdict_is_written_down_and_read_back_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a restart (or with no network) the line still describes this
    deployment instead of shrugging."""
    state = _State(monkeypatch)
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    _probe(monkeypatch, _info())
    error = CobaltError("ERROR", "auth", instance=True, needs_auth=True)

    await run_youtube_doctor(
        settings, _extractor(tmp_path), cobalt=_cobalt(error=error), pool=object()
    )
    assert state.value is not None and state.value.startswith("auth|")

    offline = await run_youtube_doctor(
        settings, _extractor(tmp_path), cobalt=_cobalt(), probe=False, pool=object()
    )

    assert _line(offline).startswith("🔴 ")
    assert "آخرین نتیجهٔ ثبت‌شده" in _line(offline)
    assert URL in _line(offline)


async def test_a_verdict_from_another_instance_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repointing ``COBALT_API_URL`` asks a new question.

    The stored answer ("🔴 wants a key") described a machine that is no longer
    configured; presenting it as the current instance's state is worse than saying
    "not tested yet", because it looks like a fresh observation.
    """
    state = _State(monkeypatch)
    state.value = "auth|https://old.example|v10|2.0|wants a key"
    settings = _settings(monkeypatch, COBALT_API_URL=URL)

    offline = await run_youtube_doctor(
        settings, _extractor(tmp_path), cobalt=_cobalt(), probe=False, pool=object()
    )
    section = _section(offline)

    assert URL in _line(offline)  # this instance, not the old one
    assert _line(offline).endswith("تست نشد"), "not a state read off another instance"
    assert "https://old.example" in section  # named, so it is not a mystery
    assert "آخرین نتیجه برای نمونهٔ دیگری" in section
    assert "🔴" not in section


# ---------------------------------------------------------------------------
# The generated cookie file: what cobalt reads, and has it read it?
# ---------------------------------------------------------------------------


class CookieCobalt:
    """A client that only knows when it started (the doctor asks nothing else)."""

    def __init__(self, started_at: float | None) -> None:
        self.started_at = started_at

    async def server_start_time(self) -> float | None:
        return self.started_at


def _cookie_cobalt(started_at: float | None) -> Any:
    """Typed as ``Any``: the fake stands in for the real client here."""
    return CookieCobalt(started_at)


def _cookie_state(tmp_path: Path, **overrides: Any) -> CobaltCookieState:
    """A generated file on disk, as ``sync_from_jar`` would have left it."""
    path = tmp_path / "cobalt" / "cookies.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"youtube": ["LOGIN_INFO=x; SAPISID=y"]}', encoding="utf-8")
    base = CobaltCookieState(path=path, cookie_count=2, generated_at=200.0)
    return replace(base, **overrides)


async def test_the_cookie_line_names_the_one_command_that_makes_it_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file generated *after* the instance booted is not the version it holds —
    and nothing else in the bot would notice: downloads work until they do not."""
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    check = await cobalt_cookie_check(
        settings, _cookie_cobalt(100.0), state=_cookie_state(tmp_path)
    )

    assert check.status == "warn"
    assert "کوبالت پیش از این نسخه بالا آمده" in check.detail
    assert COBALT_RESTART_FIX in check.detail


async def test_a_version_the_instance_has_loaded_is_a_green_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    check = await cobalt_cookie_check(
        settings, _cookie_cobalt(300.0), state=_cookie_state(tmp_path)
    )

    assert check.status == "ok"
    assert "همین نسخه را خوانده" in check.detail


async def test_without_a_probe_the_line_says_which_fact_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/blocks`` must not imply an answer it could not have asked for."""
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    check = await cobalt_cookie_check(
        settings, _cookie_cobalt(300.0), state=_cookie_state(tmp_path), probe=False
    )

    assert check.status == "warn"
    assert "زمان بالا آمدن کوبالت معلوم نشد" in check.detail


async def test_an_unknown_start_time_is_not_a_reassuring_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    check = await cobalt_cookie_check(
        settings, _cookie_cobalt(None), state=_cookie_state(tmp_path)
    )

    assert check.status == "warn"
    assert "زمان بالا آمدن کوبالت معلوم نشد" in check.detail


async def test_turned_off_and_never_generated_are_different_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = _settings(monkeypatch, COBALT_COOKIES_DIR="")
    never = _settings(monkeypatch, COBALT_COOKIES_DIR=str(tmp_path / "cobalt"))

    off_check = await cobalt_cookie_check(off, None)
    never_check = await cobalt_cookie_check(
        never,
        None,
        state=CobaltCookieState(
            path=tmp_path / "cobalt" / "cookies.json", cookie_count=24, reason="نوشته نشده"
        ),
    )

    assert off_check.status == "warn" and "تولید نمی‌شود" in off_check.detail
    assert never_check.status == "warn" and "هنوز نوشته نشده" in never_check.detail


def test_a_verdict_about_a_different_instance_is_not_evidence() -> None:
    """Same URL (modulo a slash) or the same embedded machine — nothing else counts."""
    assert FallbackHealth("ready", "https://api.cobalt.example/x").about(
        "https://api.cobalt.example/x/"
    )
    assert FallbackHealth("ready", "http://cobalt:9000").about("http://127.0.0.1:9000")
    assert FallbackHealth("ready", "http://127.0.0.1:9000").about("http://cobalt:9000")
    assert not FallbackHealth("ready", "http://cobalt:9000").about(URL)
    assert FallbackHealth("ready").about(URL), "nothing recorded is nothing to contradict"


def test_a_stored_value_that_is_not_ours_is_ignored() -> None:
    assert FallbackHealth.parse("some other bot_state value") is None
    assert FallbackHealth.parse("auth|") is None, "too few fields"

    parsed = FallbackHealth.parse("ready|https://x/|v7|1.5|")
    assert parsed is not None
    assert (parsed.state, parsed.url, parsed.dialect) == ("ready", "https://x/", "v7")
    assert parsed.remembered is True
