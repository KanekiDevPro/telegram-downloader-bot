"""What the net did with the last *real* link, and where an admin reads it.

A probe is a synthetic request: it says the instance would answer. It does not say
the instance answered the last three blocked links, and it forgets itself at the
next restart — while a quarantine expires after ten minutes. So the worker writes
down what actually happened (served it, failed, or was never asked), and both
``/doctor`` and ``/blocks`` render that evidence next to the probe's verdict. These
tests pin the record, the reasons it can hold, and the two ways it is shown: the
doctor's multi-line section and the single compact line ``/blocks`` appends to the
digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.config import Settings, cobalt_instance_is_local
from services import fallback as fallback_module
from services.cobalt import CobaltService
from services.doctor import FallbackHealth, run_youtube_doctor
from services.extractor import ExtractionError, ExtractorService, MediaInfo
from services.fallback import (
    USE_FAILED,
    USE_SKIPPED,
    USE_STATE_KEY,
    USE_USED,
    FallbackUse,
    last_use,
    remember_use,
    skip_reason,
)

URL = "https://api.cobalt.example"
BLOCKED = ExtractionError("EXTRACTOR_BLOCKED", "blocked")


class BotState:
    """``bot_state`` in memory, keyed like the table — so a use row and the
    doctor's own verdict can coexist (they are two different keys)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rows: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []

        async def set_state(pool: object, key: str, value: str) -> None:
            self.rows[key] = value
            self.writes.append((key, value))

        async def get_state(pool: object, key: str) -> str | None:
            return self.rows.get(key)

        monkeypatch.setattr(fallback_module.database, "set_state", set_state)
        monkeypatch.setattr(fallback_module.database, "get_state", get_state)


class FakeCobalt:
    """Just enough client for the health builder and the skip reasons."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        available: bool = True,
        quarantine_reason: str = "",
    ) -> None:
        self.enabled = enabled
        self.available = available
        self.quarantined = not available  # the real client derives one from the other
        self.quarantine_reason = quarantine_reason
        self.dialect: str | None = None
        self.base_url = URL

    def node_states(self) -> tuple[object, ...]:
        """A single-instance pool: the tests here are about the recorded *use*."""
        return ()


def _cobalt(**kwargs: Any) -> Any:
    """Typed as ``Any`` on purpose: the fake stands in for a real client here."""
    return FakeCobalt(**kwargs)


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


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


def _section(report: Any) -> str:
    """The fallback paragraph of a rendered report."""
    lines = report.render().splitlines()
    start = next(i for i, line in enumerate(lines) if "موتور جایگزین" in line)
    return "\n".join(lines[start : start + 6])


# ---------------------------------------------------------------------------
# The recorded use
# ---------------------------------------------------------------------------


async def test_a_real_link_is_written_down_and_read_back_with_its_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = BotState(monkeypatch)

    await remember_use(object(), USE_SKIPPED, "قرنطینه بود (ERROR: auth)")

    assert state.rows[USE_STATE_KEY].split("|")[1] == USE_SKIPPED
    use = await last_use(object())
    assert use is not None
    assert use.outcome == USE_SKIPPED
    assert use.seconds < 5, "aged at read time, not write time"
    assert use.reason == "قرنطینه بود (ERROR: auth)"
    assert not use.ok


async def test_a_net_that_never_had_to_work_has_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    BotState(monkeypatch)

    assert await last_use(object()) is None


def test_a_row_that_is_not_ours_is_ignored() -> None:
    """The doctor's own verdict lives in the same table — it must not be read as
    a use row (and an unreadable one must not crash a report)."""
    assert FallbackUse.parse("ready|https://api.cobalt.example|v7|1.5|") is None
    assert FallbackUse.parse("not ours") is None
    assert FallbackUse.parse("nonsense|used|reason") is None
    assert FallbackUse.parse("1|used") is None, "too few fields"


async def test_recording_is_harmless_when_the_row_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure path must not fail harder because telemetry is down."""

    async def broken(pool: object, key: str, value: str) -> None:
        raise RuntimeError("no database")

    monkeypatch.setattr(fallback_module.database, "set_state", broken)

    await remember_use(object(), USE_USED)  # does not raise


# ---------------------------------------------------------------------------
# The reasons a blocked link was not handed over
# ---------------------------------------------------------------------------


def test_a_skipped_net_names_what_stopped_it() -> None:
    quarantined = _cobalt(available=False, quarantine_reason="ERROR: instance is down")

    assert skip_reason(BLOCKED, CobaltService("")) == "خاموش بود (COBALT_API_URL خالی)"
    assert skip_reason(BLOCKED, _cobalt(enabled=False)) == "خاموش بود (COBALT_API_URL خالی)"
    assert skip_reason(BLOCKED, quarantined) == "قرنطینه بود (ERROR: instance is down)"
    assert skip_reason(BLOCKED, _cobalt(available=False)) == "قرنطینه بود"
    assert skip_reason(BLOCKED, None) == "کلاینت fallback در این اجرا ساخته نشده بود"


def test_a_failure_the_net_was_never_for_is_not_a_skip() -> None:
    """Half the truth is worse than none: a private video would not have been
    handed to the fallback either way, so nothing is written down."""
    assert skip_reason(ExtractionError("PRIVATE_VIDEO", ""), _cobalt()) == ""
    assert skip_reason(ExtractionError("LIVE_STREAM", ""), None) == ""
    # ...and when it *is* about to be used, there is nothing to report.
    assert skip_reason(BLOCKED, _cobalt()) == ""


# ---------------------------------------------------------------------------
# What the doctor shows
# ---------------------------------------------------------------------------


async def test_the_doctor_section_reports_the_last_real_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    BotState(monkeypatch)
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    await remember_use(object(), USE_FAILED, "UNREACHABLE: instance is down")
    extractor = ExtractorService(tmp_path, cookie_file=None, js_runtime="none")

    report = await run_youtube_doctor(
        settings, extractor, cobalt=_cobalt(), probe=False, pool=object()
    )

    section = _section(report)
    assert "آخرین لینک بلاک‌شده" in section
    assert "❌ خودش هم نشد" in section
    assert "UNREACHABLE: instance is down" in section


async def test_a_doctor_run_without_a_record_invents_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    BotState(monkeypatch)
    settings = _settings(monkeypatch, COBALT_API_URL=URL)
    extractor = ExtractorService(tmp_path, cookie_file=None, js_runtime="none")

    report = await run_youtube_doctor(
        settings, extractor, cobalt=_cobalt(), probe=False, pool=object()
    )

    assert "آخرین لینک بلاک‌شده" not in _section(report)


# ---------------------------------------------------------------------------
# The one line /blocks appends
# ---------------------------------------------------------------------------


def test_the_compact_line_names_the_instance_and_the_state() -> None:
    line = FallbackHealth("auth", URL, "v10", seconds=1.9).line()

    assert line.startswith("🔌 موتور جایگزین: https://api.cobalt.example — 🔴 نیازمند کلید")
    assert "v10" in line
    assert "COBALT_API_KEY" in line, "and the fix an admin should try"
    assert "/doctor" not in line, "a live probe needs no further asking"


def test_an_off_net_is_reported_as_a_choice_not_a_fault() -> None:
    line = FallbackHealth("off").line()

    assert "⚫️ خاموش" in line
    assert "قدم بعدی" not in line, "there is nothing to fix about an optional safety net"
    assert "/doctor" not in line


def test_a_remembered_verdict_asks_for_a_fresh_probe() -> None:
    line = FallbackHealth("ready", URL, "v10", remembered=True).line()

    assert "🟢 آماده به کار" in line
    assert "آخرین نتیجهٔ ثبت‌شده" in line
    assert "/doctor" in line, "an old green is not a promise"


def test_a_live_quarantine_is_not_called_stale() -> None:
    """A quarantine from this process is fresh evidence — the reason is shown and
    the admin is not sent to /doctor to ask the same question again."""
    line = FallbackHealth("quarantined", URL, reason="ERROR: instance is down").line()

    assert line.splitlines()[0].startswith("🔌 موتور جایگزین: https://api.cobalt.example — 🟡")
    assert "ERROR: instance is down" in line
    assert "/doctor" not in line


def test_the_last_real_use_is_the_paragraph_that_decides_it() -> None:
    health = FallbackHealth(
        "ready",
        URL,
        "v10",
        use=FallbackUse(USE_SKIPPED, seconds=300, reason="قرنطینه بود (ERROR: auth)"),
    )

    line = health.line()

    assert "⏭ رد شد (استفاده نشد)" in line
    assert "5 دقیقه پیش" in line
    assert "قرنطینه بود (ERROR: auth)" in line


def test_the_same_reason_is_not_said_twice() -> None:
    """When the probe and the last real link failed for the same reason, the
    compact line says it once."""
    health = FallbackHealth(
        "quarantined",
        URL,
        reason="ERROR: auth",
        use=FallbackUse(USE_FAILED, seconds=60, reason="ERROR: auth"),
    )

    assert health.line().count("ERROR: auth") == 1
    assert "❌ خودش هم نشد" in health.line()


def test_the_embedded_instance_is_named_as_the_one_we_run() -> None:
    """Where the instance lives decides what a broken fallback *means* — the embedded
    one shares the host's address, so its fixes are a session or a proxy, not a
    different address."""
    embedded = FallbackHealth("ready", "http://cobalt:9000", "v10", seconds=0.4).line()
    external = FallbackHealth("ready", "https://api.cobalt.example", "v10").line()

    assert embedded.startswith("🔌 موتور جایگزین (داخلی): http://cobalt:9000 — 🟢")
    # Said once, not twice: the label carries it, the URL does not repeat it.
    assert embedded.count("داخلی") == 1
    assert external.startswith("🔌 موتور جایگزین: https://api.cobalt.example — 🟢")
    assert "داخلی" not in external
    assert FallbackHealth("ready", "http://cobalt:9000").embedded


def test_a_lan_or_public_instance_is_not_called_internal() -> None:
    """"Somewhere else" is the whole point of the word — a box on the LAN or on the
    internet is not the instance this stack runs."""
    assert cobalt_instance_is_local("http://cobalt:9000")
    assert cobalt_instance_is_local("http://127.0.0.1:9000")
    assert cobalt_instance_is_local("http://127.0.0.1:9000/")
    assert not cobalt_instance_is_local("https://api.cobalt.tools")
    assert not cobalt_instance_is_local("http://192.168.1.5:9000")
    assert not cobalt_instance_is_local("")


def test_the_report_still_spells_everything_out() -> None:
    """The doctor keeps the long form: the /blocks one-liner must not have cost it
    the reasons or the fix."""
    health = FallbackHealth(
        "auth", URL, "v10", seconds=1.9, reason="error.api.auth.jwt.missing"
    )

    detail = health.detail()

    assert "علت: error.api.auth.jwt.missing" in detail
    assert "نیازمند کلید احراز هویت" in detail
    assert "قدم بعدی" in detail
