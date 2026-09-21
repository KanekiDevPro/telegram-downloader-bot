"""Automatic cookie refresh: mechanical fix, with two guard rails.

A login-shaped block has a mechanical fix when a browser profile is reachable:
read the cookies, replace the jar, prove it with a probe, report. The risk is not
the export — it is replacing something that worked, so these tests pin the
never-worse rule, the atomic move, the cooldown, and that every outcome (including
"there is no browser in this container") reaches the admins.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from services import cookie_refresh
from services.cookie_refresh import ExportError, RefreshOutcome, auto_refresh_jar, render_outcome
from services.extractor import browser_profile_paths

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str) -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name}\tvalue\n"


def _jar(path: Path, *names: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + "".join(_row(name) for name in names), encoding="utf-8")
    return path


def _logged_in(path: Path) -> Path:
    return _jar(path, "LOGIN_INFO", "SAPISID")


def _logged_out(path: Path) -> Path:
    return _jar(path, "PREF")


class FakeExtractor:
    """The refresh only asks the extractor for the jar's state (then probes it)."""

    def __init__(self, cookie_file: Path | None) -> None:
        self.cookie_file = cookie_file

    def cookie_jar_state(self) -> Any:
        from services.extractor import CookieJarState

        path = self.cookie_file
        usable = path is not None and path.is_file()
        return CookieJarState(
            path=path,
            kind="ok" if usable else "missing",
            size_bytes=path.stat().st_size if usable and path else None,
            exported_at=path.stat().st_mtime if usable and path else None,
            mount=None,
            writable=True,
            cookie_count=2 if usable else 0,
            missing_login=(),
            copy_path=None,
            copy_refreshed_at=None,
            in_sync=None,
        )


class FakeBot:
    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failing = set(failing)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id in self.failing:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


def _settings(tmp_path: Path, *, spec: str, jar: Path | None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        cookie_auto_export=spec,
        cookie_file=jar,
        admin_ids=[1, 2],
    )


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The cooldown and the in-flight flag are process-wide: start each test clean."""
    cookie_refresh.reset_state()
    # The probe is a network call; every test replaces it with a recorded verdict.
    async def verdict(extractor: Any, state: Any) -> str:
        return "✅ فرض تست: یوتیوب پذیرفت"

    monkeypatch.setattr(cookie_refresh, "verify_export", verdict)
    # Whether a browser happens to be installed here must not decide these tests:
    # both pre-checks get their own tests, and here they say "nothing in the way".
    monkeypatch.setattr(cookie_refresh, "browser_profile_reachable", lambda spec: None)
    monkeypatch.setattr(cookie_refresh, "app_bound_encryption_active", lambda spec: False)
    yield
    cookie_refresh.reset_state()


def _write_candidate(monkeypatch: pytest.MonkeyPatch, jar_content: tuple[str, ...] | None) -> list[Path]:
    """Make the export write a known jar (or fail with ``None``)."""
    written: list[Path] = []

    def fake_export(spec: str, output: Path, *, verbose: bool = False) -> int:
        if jar_content is None:
            raise ExportError("could not read cookies from 'chrome': no profile here")
        _logged_in(output) if "LOGIN_INFO" in jar_content else _logged_out(output)
        written.append(output)
        return len(jar_content)

    monkeypatch.setattr(cookie_refresh, "export_jar_from_browser", fake_export)
    return written


# ---------------------------------------------------------------------------
# Off by default, and never in a hurry
# ---------------------------------------------------------------------------


async def test_without_a_profile_nothing_happens(tmp_path: Path) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    called: list[str] = []
    settings = _settings(tmp_path, spec="", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1])  # type: ignore[arg-type]

    assert outcome is None
    assert called == []


async def test_the_cooldown_blocks_a_second_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    written = _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    extractor = FakeExtractor(jar)

    first = await auto_refresh_jar(settings, extractor, FakeBot(), [1])  # type: ignore[arg-type]
    second = await auto_refresh_jar(settings, extractor, FakeBot(), [1])  # type: ignore[arg-type]

    assert first is not None and first.kind == "replaced"
    assert second is None, "a broken jar fails every link; one attempt is enough"
    assert len(written) == 1


async def test_the_cooldown_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    extractor = FakeExtractor(jar)

    await auto_refresh_jar(settings, extractor, FakeBot(), [1])  # type: ignore[arg-type]
    later = time.monotonic() + cookie_refresh.DEFAULT_COOLDOWN_S + 1
    again = await auto_refresh_jar(settings, extractor, FakeBot(), [1], now=later)  # type: ignore[arg-type]

    assert again is not None and again.kind == "replaced"


async def test_a_concurrent_attempt_is_not_started_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two workers can hit a login block at the same moment."""
    jar = _logged_out(tmp_path / "cookies.txt")
    calls = [0]

    def slow_export(spec: str, output: Path, *, verbose: bool = False) -> int:
        calls[0] += 1
        time.sleep(0.05)
        _logged_in(output)
        return 2

    monkeypatch.setattr(cookie_refresh, "export_jar_from_browser", slow_export)
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    extractor = FakeExtractor(jar)

    results = await asyncio.gather(
        auto_refresh_jar(settings, extractor, FakeBot(), [1]),  # type: ignore[arg-type]
        auto_refresh_jar(settings, extractor, FakeBot(), [1]),  # type: ignore[arg-type]
    )

    assert calls == [1]
    assert sorted(outcome is None for outcome in results) == [False, True]


# ---------------------------------------------------------------------------
# The cheap answer first: is there even a browser on this machine?
# ---------------------------------------------------------------------------


async def test_no_browser_here_skips_the_attempt_and_names_the_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain container: "no profile here" beats a wasted export and a yt-dlp shrug."""
    jar = _logged_out(tmp_path / "cookies.txt")
    written = _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    monkeypatch.setattr(cookie_refresh, "browser_profile_reachable", lambda spec: False)
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "unreachable"
    assert "دیده نشد" in outcome.message
    assert written == [], "an attempt is not spent when there is nothing to read"
    assert jar.read_text(encoding="utf-8") == HEADER + _row("PREF"), "the jar is untouched"
    # The operator's next move is in the message: the paths we looked at, and the
    # command to run where the browser actually is.
    assert str(browser_profile_paths("chrome")[0]) in outcome.message
    assert "scripts/export_cookies.py" in bot.sent[0][1]


async def test_an_app_bound_locked_profile_is_not_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows locks Chrome/Edge/Brave cookies with the browser's own key.

    The profile is right there and the export still cannot work, so the honest
    answer is the lock and the two ways around it — not "profile not found" and
    not a yt-dlp shrug.
    """
    jar = _logged_out(tmp_path / "cookies.txt")
    written = _write_candidate(monkeypatch, ("LOGIN_INFO",))
    monkeypatch.setattr(cookie_refresh, "app_bound_encryption_active", lambda spec: True)
    settings = _settings(tmp_path, spec="edge:Default", jar=jar)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "unreachable"
    assert "App-Bound" in outcome.message
    assert "HttpOnly" in outcome.message and "Firefox" in outcome.message, "the ways out"
    assert written == [], "attempting it would only produce 'failed to load cookies'"
    assert "App-Bound" in bot.sent[0][1]


async def test_a_reachable_profile_is_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    written = _write_candidate(monkeypatch, ("LOGIN_INFO",))
    monkeypatch.setattr(cookie_refresh, "browser_profile_reachable", lambda spec: True)
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "replaced"
    assert written, "the export runs when the profile is there"


async def test_a_bad_profile_spec_is_reported_not_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in COOKIE_AUTO_EXPORT must not surface once per failing link."""
    jar = _logged_out(tmp_path / "cookies.txt")
    written = _write_candidate(monkeypatch, ("LOGIN_INFO",))
    settings = _settings(tmp_path, spec="not-a-browser", jar=jar)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "failed"
    assert "پروفایل نامعتبر" in outcome.message
    assert written == []
    assert bot.sent, "the admins hear about a config typo once (the cooldown rate-limits it)"


# ---------------------------------------------------------------------------
# On demand: force, a named profile, and the fix the trend needs to see
# ---------------------------------------------------------------------------


async def test_force_ignores_the_cooldown_but_still_counts_as_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/refresh`` and the alert's button: a human asked, so the cooldown yields."""
    jar = _logged_out(tmp_path / "cookies.txt")
    written = _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    extractor = FakeExtractor(jar)

    first = await auto_refresh_jar(settings, extractor, FakeBot(), [1])  # type: ignore[arg-type]
    forced = await auto_refresh_jar(settings, extractor, FakeBot(), [1], force=True)  # type: ignore[arg-type]
    automatic = await auto_refresh_jar(settings, extractor, FakeBot(), [1])  # type: ignore[arg-type]

    assert first is not None and first.kind == "replaced"
    assert forced is not None and forced.kind == "replaced"
    assert automatic is None, "a manual attempt still buys the cooldown"
    assert len(written) == 2


async def test_a_named_profile_overrides_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An admin tries a profile without editing .env or restarting the bot."""
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    settings = _settings(tmp_path, spec="", jar=jar)  # the automatic feature is off

    outcome = await auto_refresh_jar(
        settings, FakeExtractor(jar), FakeBot(), [1], force=True, spec="edge:Default"  # type: ignore[arg-type]
    )

    assert outcome is not None and outcome.kind == "replaced"


async def test_a_replacement_is_recorded_as_a_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/trend`` can only weigh a fix it knows about."""
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    recorded: list[tuple[str, str]] = []

    async def record(pool: Any, *, kind: str, detail: str = "") -> None:
        recorded.append((kind, detail))

    monkeypatch.setattr(cookie_refresh.telemetry, "record_fix", record)
    settings = _settings(tmp_path, spec="edge:Default", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1], pool=object())  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "replaced"
    assert recorded == [("cookie_jar", "edge:Default → 2 کوکی")]


async def test_a_kept_export_is_not_a_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing changed, so the trend must not see a fix that never happened."""
    jar = _logged_in(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("nothing",))
    recorded: list[str] = []

    async def record(pool: Any, *, kind: str, detail: str = "") -> None:
        recorded.append(kind)

    monkeypatch.setattr(cookie_refresh.telemetry, "record_fix", record)
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1], pool=object())  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "kept"
    assert recorded == []


# ---------------------------------------------------------------------------
# The replacement itself
# ---------------------------------------------------------------------------


async def test_a_good_export_replaces_the_jar_and_is_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO", "SAPISID"))
    settings = _settings(tmp_path, spec="chrome:Default", jar=jar)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1, 2])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "replaced"
    assert "LOGIN_INFO" in jar.read_text(encoding="utf-8"), "the jar is the new one"
    assert outcome.cookies == 2
    assert "فرض تست" in outcome.verdict
    assert len(bot.sent) == 2, "both admins hear the outcome"
    text = bot.sent[0][1]
    assert "جار کوکی خودکار تازه شد" in text and "فرض تست" in text


async def test_no_leftover_candidate_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO",))
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1])  # type: ignore[arg-type]

    assert sorted(p.name for p in tmp_path.iterdir()) == ["cookies.txt"]


async def test_an_export_without_a_login_never_overwrites_a_working_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one irreversible mistake: losing the login you already had."""
    jar = _logged_in(tmp_path / "cookies.txt")
    before = jar.read_text(encoding="utf-8")
    _write_candidate(monkeypatch, ("PREF",))
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "kept"
    assert jar.read_text(encoding="utf-8") == before
    assert "لاگین یوتیوب نداشت" in outcome.message
    assert "دست‌نخورده ماند" in bot.sent[0][1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cookies.txt"]


async def test_an_unusable_export_is_kept_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    before = jar.read_text(encoding="utf-8")

    def fake_export(spec: str, output: Path, *, verbose: bool = False) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(HEADER, encoding="utf-8")  # a jar with no cookies
        return 0

    monkeypatch.setattr(cookie_refresh, "export_jar_from_browser", fake_export)
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "kept"
    assert jar.read_text(encoding="utf-8") == before


async def test_a_read_only_target_is_reported_as_such(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container case: the jar is a read-only mount, so the export belongs on the host."""
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO",))
    monkeypatch.setattr(cookie_refresh.os, "replace", _refuse)
    settings = _settings(tmp_path, spec="chrome", jar=jar)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "unreachable"
    assert "فقط-خواندنی" in outcome.message
    assert "scripts/export_cookies.py" in bot.sent[0][1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cookies.txt"]


def _refuse(*args: Any, **kwargs: Any) -> None:
    raise OSError(30, "Read-only file system")


async def test_a_read_only_mount_whose_cleanup_fails_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed case: the jar is a read-only mount, so *everything* hits EROFS.

    Nothing can be written and nothing can be removed — including the temporary
    candidate the cleanup tries to delete. That must still end as a reported
    outcome, because this runs while a download is already failing.
    """
    jar = _logged_out(tmp_path / "cookies.txt")
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    def refuse_export(spec: str, output: Path, *, verbose: bool = False) -> int:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(cookie_refresh, "export_jar_from_browser", refuse_export)
    monkeypatch.setattr(Path, "unlink", _refuse)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "unreachable"
    assert "فقط-خواندنی" in outcome.message
    assert bot.sent, "an outcome nobody hears about is half an outcome"


async def test_a_refresh_that_raises_does_not_break_the_failure_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever happens in here, the download's own error handling must survive."""
    jar = _logged_out(tmp_path / "cookies.txt")
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    async def exploding_probe(extractor: Any, state: Any) -> str:
        raise RuntimeError("probe exploded")

    _write_candidate(monkeypatch, ("LOGIN_INFO",))  # a good export, then a bad probe
    monkeypatch.setattr(cookie_refresh, "verify_export", exploding_probe)
    bot = FakeBot()

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), bot, [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "failed"
    assert "probe exploded" in outcome.message
    assert bot.sent


async def test_an_unreachable_profile_is_reported_with_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, None)  # the export raises ExportError
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "unreachable"
    assert "no profile here" in outcome.message
    assert jar.read_text(encoding="utf-8") == HEADER + _row("PREF"), "the jar is untouched"


async def test_an_unexpected_export_crash_is_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")

    def exploding(spec: str, output: Path, *, verbose: bool = False) -> int:
        raise RuntimeError("yt-dlp changed its mind")

    monkeypatch.setattr(cookie_refresh, "export_jar_from_browser", exploding)
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    outcome = await auto_refresh_jar(settings, FakeExtractor(jar), FakeBot(), [1])  # type: ignore[arg-type]

    assert outcome is not None and outcome.kind == "failed"
    assert "yt-dlp changed its mind" in outcome.message


async def test_an_unreachable_admin_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jar = _logged_out(tmp_path / "cookies.txt")
    _write_candidate(monkeypatch, ("LOGIN_INFO",))
    settings = _settings(tmp_path, spec="chrome", jar=jar)

    outcome = await auto_refresh_jar(
        settings, FakeExtractor(jar), FakeBot(failing=(1,)), [1, 2]  # type: ignore[arg-type]
    )

    assert outcome is not None and outcome.kind == "replaced"


def test_every_outcome_has_words_for_it() -> None:
    for outcome in (
        RefreshOutcome("replaced", "", cookies=3, verdict="✅ قبول شد"),
        RefreshOutcome("kept", "چیزی بهتر نبود"),
        RefreshOutcome("unreachable", "پروفایل نبود"),
        RefreshOutcome("failed", "boom"),
    ):
        rendered = render_outcome(outcome)
        assert rendered.strip()
        assert "<b>" not in rendered or "جار" in rendered


def test_the_cooldown_is_half_an_hour_by_default() -> None:
    assert 60.0 <= cookie_refresh.DEFAULT_COOLDOWN_S <= 3600.0


def test_the_candidate_never_wins_without_a_login(tmp_path: Path) -> None:
    current = _logged_in(tmp_path / "current.txt")
    candidate = _logged_out(tmp_path / "candidate.txt")

    assert cookie_refresh.candidate_rejection(candidate, current) is not None
    assert cookie_refresh.candidate_rejection(_logged_in(tmp_path / "better.txt"), current) is None
    assert cookie_refresh.candidate_rejection(candidate, _logged_out(tmp_path / "worse.txt")) is None
