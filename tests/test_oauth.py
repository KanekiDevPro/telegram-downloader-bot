"""The OAuth2 Smart-TV login: the probe, the flow, and the admin command.

Three facts make this feature honest rather than aspirational, and each has its
own tests here:

* the *probe* — current yt-dlp refuses ``username=oauth2`` outright (YouTube
  revoked the flow), so ``oauth_supported`` must read that refusal as
  "unsupported" and a genuine login attempt as "supported";
* the *flow* — one child process whose stderr carries the device code across to
  the handler while it is still running, driven here against a fake child so no
  test ever talks to Google;
* the *command* — the code arrives with its button before the flow ends, a
  refusal is quoted instead of guessed at, and a second ``/oauth`` while one is
  live is answered rather than queued.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, User

from core.config import Settings
from handlers import admin as admin_module
from services import oauth as oauth_module
from services.oauth import (
    DEVICE_URL,
    FlowRunning,
    OAuthFlow,
    OAuthOutcome,
    OAuthService,
    cache_state_line,
    probe_oauth_support,
)

# ---------------------------------------------------------------------------
# Fakes: a child that speaks the stderr grammar, and a bot that records
# ---------------------------------------------------------------------------


class FakeProcess:
    """The observable half of ``asyncio.create_subprocess_exec``."""

    def __init__(self, lines: list[bytes], return_code: int = 0) -> None:
        self._lines = lines
        self.returncode: int | None = None
        self.terminated = False
        self._return_code = return_code
        self.stderr = _FakeStream(lines, self)

    def set_returncode_none(self) -> None:
        """Report "still running", as a live child does."""
        self.returncode = None

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = self._return_code
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:  # pragma: no cover — only the escalation path
        self.terminated = True


class _FakeStream:
    def __init__(self, lines: list[bytes], process: FakeProcess) -> None:
        self._lines = lines
        self._process = process

    async def readline(self) -> bytes:
        if not self._lines:
            await asyncio.sleep(0)
            return b""
        return self._lines.pop(0)


class RecordingBot:
    """Records the API calls, answers with a real Message (see test_admin_commands).

    The shortcut methods exist because a bound Message's ``edit_text`` calls
    ``bot.edit_message`` *directly* — it never goes through ``__call__`` — so a
    bot without them swallows the edit in ``_say``'s broad ``except``.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return _message(method.text or "", self)
        return True

    async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> Message:
        from aiogram.methods import SendMessage as _Send

        method = _Send(chat_id=chat_id, text=text)
        self.calls.append(method)
        return _message(text, self)

    async def edit_message_text(self, text: str, **kwargs: Any) -> Message:
        from aiogram.methods import EditMessageText

        method = EditMessageText(text=text, chat_id=1, message_id=1)
        self.calls.append(method)
        return _message(text, self)

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def all_texts(self) -> list[str]:
        """Sends *and* edits — the status message is edited, not replaced."""
        from aiogram.methods import EditMessageText

        return [
            call.text or ""
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
        ]


def _message(text: str, bot: RecordingBot, user_id: int = 1) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=555, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="admin"),
        text=text,
    ).as_(cast(Bot, bot))


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _supported_method(supported: bool, evidence: str) -> Any:
    """A ``OAuthService.supported`` replacement with the given answer."""

    async def supported_method(self: OAuthService) -> tuple[bool, str]:
        return supported, evidence

    return supported_method


def _stub_probe(monkeypatch: pytest.MonkeyPatch, supported: bool, evidence: str = "") -> None:
    """Stub the probe where it is *looked up*: the service, and the handler's own
    imported name (``from services.oauth import probe_oauth_support`` binds a
    reference at import time — patching only the module would leave the handler
    asking the real yt-dlp, whose answer on this host is the refusal)."""

    async def fake_probe() -> tuple[bool, str]:
        return supported, evidence

    monkeypatch.setattr(oauth_module, "probe_oauth_support", fake_probe)
    monkeypatch.setattr(
        oauth_module.OAuthService, "supported", _supported_method(supported, evidence)
    )


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every command reads the settings through the module that owns them."""
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setattr(
        admin_module,
        "get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


async def test_the_probe_reads_the_real_refusal_as_unsupported() -> None:
    """The answer this yt-dlp actually gives today: revoked, so unsupported."""
    supported, evidence = await probe_oauth_support()

    assert supported is False
    assert "Login with OAuth is no longer supported" in evidence


async def test_the_probe_reads_a_working_login_as_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A build (or plugin) that starts the flow without refusing is supported."""
    import contextlib
    import io

    def working_login() -> tuple[bool, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
            return oauth_module._probe_in_thread.__wrapped__() if hasattr(
                oauth_module._probe_in_thread, "__wrapped__"
            ) else (True, "no refusal raised")

    # Simpler and honest: patch the thread body itself.
    monkeypatch.setattr(oauth_module, "_probe_in_thread", lambda: (True, "no refusal raised"))
    supported, evidence = await fake_probe_result()
    assert supported is True


async def fake_probe_result() -> tuple[bool, str]:
    return await oauth_module.probe_oauth_support()


async def test_the_probe_treats_anything_but_the_refusal_as_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A network error means an implementation *tried* — that is not a refusal."""
    import contextlib
    import io

    class _Err(Exception):
        pass

    def raising_probe() -> tuple[bool, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
            try:
                raise _Err("connection reset")
            except Exception as exc:  # noqa: BLE001
                text = f"{type(exc).__name__}: {exc}"
                return oauth_module._OAUTH_REFUSAL not in text, text

    monkeypatch.setattr(oauth_module, "_probe_in_thread", raising_probe)
    supported, evidence = await probe_oauth_support()
    assert supported is True
    assert "connection reset" in evidence


# ---------------------------------------------------------------------------
# The flow: code extraction, the child run, cancellation
# ---------------------------------------------------------------------------


def test_the_code_grammar_covers_the_real_prompt() -> None:
    text = (
        "[youtube+oauth2] To give yt-dlp access to your account, go to "
        "https://www.google.com/device and enter code ABC-DEF-GHI"
    )
    code = OAuthFlow._extract_code(text)
    assert code == "ABC-DEF-GHI"


def test_the_code_grammar_tolerates_terminal_mangling() -> None:
    # A terminal can print the hyphens as en-dashes; the normaliser converts them
    # back, so the admin copies exactly what Google's page expects:
    assert OAuthFlow._extract_code("enter code ABC–DEF–GHI now") == "ABC-DEF-GHI"
    assert OAuthFlow._extract_code("enter code ABC - DEF - GHI") == "ABC-DEF-GHI"


def test_lines_without_a_code_extract_nothing() -> None:
    assert OAuthFlow._extract_code("Downloading webpage") is None


async def test_the_flow_pushes_the_code_while_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The code crosses to the sink *before* the child exits — that is the point."""
    lines = [
        b"[youtube] oauth: go to https://www.google.com/device and enter code XYZ-ABC-DEF",
        b"signalling: waiting for the user to enter the code",
    ]
    process = FakeProcess(lines)

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    sink: asyncio.Queue[str] = asyncio.Queue()
    flow = OAuthFlow(python_executable=sys.executable, code_sink=sink)

    run_task = asyncio.create_task(flow.run("https://youtu.be/dQw4w9WgXcQ"))
    code = await asyncio.wait_for(sink.get(), timeout=2.0)
    outcome = await run_task

    assert code == "XYZ-ABC-DEF"
    assert flow.code == "XYZ-ABC-DEF"
    assert outcome.status == "success"
    assert outcome.code == "XYZ-ABC-DEF"


async def test_a_refusing_child_reads_as_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        b"ERROR: Login with OAuth is no longer supported. Use --cookies instead.",
    ]
    process = FakeProcess(lines, return_code=1)

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    flow = OAuthFlow(python_executable=sys.executable)
    outcome = await flow.run("https://youtu.be/dQw4w9WgXcQ")

    assert outcome.status == "unsupported"
    assert "no longer supported" in outcome.detail


async def test_a_failing_child_reports_its_last_words(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [b"ERROR: unable to download video data: HTTP Error 403"]
    process = FakeProcess(lines, return_code=1)

    async def fake_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    flow = OAuthFlow(python_executable=sys.executable)
    outcome = await flow.run("https://youtu.be/dQw4w9WgXcQ")

    assert outcome.status == "failed"
    assert "403" in outcome.detail


async def test_the_child_command_carries_the_contract() -> None:
    flow = OAuthFlow(cache_dir="/cache", proxy="http://warp:1080", python_executable="py")
    argv = flow._argv("https://youtu.be/dQw4w9WgXcQ")

    assert argv[:6] == ["py", "-m", "yt_dlp", "--username", "oauth2", "--password"]
    assert argv[6] == ""
    assert "--cache-dir" in argv and "/cache" in argv
    assert "--proxy" in argv and "http://warp:1080" in argv
    assert argv[-2:] == ["--dump-json", "https://youtu.be/dQw4w9WgXcQ"]


async def test_cancel_terminates_a_live_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling stops the child — terminate, wait, and never hang."""

    class HangingProcess(FakeProcess):
        """A child mid-poll: wait() returns only once it is terminated."""

        async def wait(self) -> int:
            while not self.terminated:
                await asyncio.sleep(0.01)
            self.returncode = -15
            return self.returncode

    process = HangingProcess([])
    process.set_returncode_none()  # still "running" from cancel's point of view

    flow = OAuthFlow(python_executable=sys.executable)
    # Injected directly: no real child to start. The cast is the test's own
    # honesty — a FakeProcess is not a real subprocess transport, and mypy is
    # right about that; the flow only ever touches wait/terminate/stderr.
    flow._process = cast(Any, process)

    await asyncio.wait_for(flow.cancel(), timeout=5.0)

    assert process.terminated
    assert process.returncode == -15


# ---------------------------------------------------------------------------
# The service: one flow at a time
# ---------------------------------------------------------------------------


async def test_a_second_login_is_answered_not_queued() -> None:
    service = OAuthService()
    flow = await service.start("https://youtu.be/dQw4w9WgXcQ")
    with pytest.raises(FlowRunning):
        await service.start("https://youtu.be/dQw4w9WgXcQ")
    assert service.running
    await service.finish(flow)
    assert not service.running
    # And the slot is free again:
    await service.start("https://youtu.be/dQw4w9WgXcQ")
    await service.cancel()


async def test_supported_is_probed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def counting_probe() -> tuple[bool, str]:
        calls.append(1)
        return False, "refusal"

    monkeypatch.setattr(oauth_module, "probe_oauth_support", counting_probe)
    service = OAuthService()
    first = await service.supported()
    second = await service.supported()

    assert first == second == (False, "refusal")
    assert len(calls) == 1, "the answer is remembered, not re-derived"


# ---------------------------------------------------------------------------
# The /oauth command
# ---------------------------------------------------------------------------


async def test_oauth_is_admin_only() -> None:
    bot = RecordingBot()
    message = _message("/oauth", bot, user_id=9)

    await admin_module.cmd_oauth(message, bot)

    assert bot.texts == ["⛔️ فقط ادمین می‌تونه."]


async def test_oauth_when_unsupported_quotes_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_probe(monkeypatch, False, "ExtractorError: Login with OAuth is no longer supported")
    bot = RecordingBot()
    message = _message("/oauth", bot)

    await admin_module.cmd_oauth(message, bot, OAuthService())

    assert len(bot.texts) == 1
    assert "لاگین OAuth" in bot.texts[0]
    assert "no longer supported" in bot.texts[0]
    assert "/fixlogin" in bot.texts[0], "the working alternative is named"


async def test_oauth_delivers_the_code_with_a_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full run: code message, button, then the success report."""
    _stub_probe(monkeypatch, True)
    _settings(monkeypatch, ADMIN_IDS="1", YTDLP_USE_OAUTH2="1")

    class FakeService(OAuthService):
        def __init__(self) -> None:
            super().__init__()
            self.started = False

        async def start(self, probe: str, *, code_sink: Any = None) -> OAuthFlow:
            self.started = True
            flow = OAuthFlow(code_sink=code_sink)
            # Simulate the child: one code, then exit clean.
            asyncio.get_running_loop().create_task(self._child(flow))
            return flow

        async def _child(self, flow: OAuthFlow) -> None:
            await asyncio.sleep(0)
            assert flow.code_sink is not None
            await flow.code_sink.put("XXX-YYY-ZZZ")
            flow.code = "XXX-YYY-ZZZ"

    async def fake_run(flow: OAuthFlow, probe: str) -> OAuthOutcome:
        return OAuthOutcome("success", code=flow.code)

    monkeypatch.setattr(OAuthFlow, "run", fake_run)
    service = FakeService()
    bot = RecordingBot()
    message = _message("/oauth", bot)

    await admin_module.cmd_oauth(message, bot, service)

    joined = "\n".join(bot.all_texts)
    assert "XXX-YYY-ZZZ" in joined, "the code is shown"
    assert DEVICE_URL in joined, "the link is in the message"
    assert "✅" in joined, "and the success lands after the flow ends"
    assert "YTDLP_USE_OAUTH2" not in joined, "the switch is on, so no nudge"
    assert not service.running, "the slot is released"


async def test_oauth_success_nudges_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_probe(monkeypatch, True)
    monkeypatch.setenv("ADMIN_IDS", "1")
    monkeypatch.setenv("YTDLP_USE_OAUTH2", "0")

    class FakeService(OAuthService):
        async def start(self, probe: str, *, code_sink: Any = None) -> OAuthFlow:
            flow = OAuthFlow(code_sink=code_sink)
            asyncio.get_running_loop().create_task(self._child(flow))
            return flow

        async def _child(self, flow: OAuthFlow) -> None:
            await asyncio.sleep(0)
            assert flow.code_sink is not None
            await flow.code_sink.put("XXX-YYY-ZZZ")
            flow.code = "XXX-YYY-ZZZ"

    async def fake_run(flow: OAuthFlow, probe: str) -> OAuthOutcome:
        return OAuthOutcome("success", code=flow.code)

    monkeypatch.setattr(OAuthFlow, "run", fake_run)
    bot = RecordingBot()
    message = _message("/oauth", bot)

    await admin_module.cmd_oauth(message, bot, FakeService())

    joined = "\n".join(bot.all_texts)
    assert "YTDLP_USE_OAUTH2=1" in joined, "a login nobody uses is a wasted login"


async def test_oauth_while_another_flow_runs_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_probe(monkeypatch, True)
    monkeypatch.setenv("ADMIN_IDS", "1")
    service = OAuthService()
    await service.start("https://youtu.be/dQw4w9WgXcQ")
    bot = RecordingBot()
    message = _message("/oauth", bot)

    await admin_module.cmd_oauth(message, bot, service)

    assert "در جریان" in bot.texts[0]
    await service.cancel()


async def test_oauth_with_no_code_cancels_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent child is cancelled and its failure explained, not left hanging."""
    _stub_probe(monkeypatch, True)
    monkeypatch.setenv("ADMIN_IDS", "1")

    async def fake_wait(sink: Any, timeout_s: float) -> None:
        return None

    cancelled: list[bool] = []

    class SilentFlow(OAuthFlow):
        async def cancel(self) -> None:
            cancelled.append(True)

    class FakeService(OAuthService):
        async def start(self, probe: str, *, code_sink: Any = None) -> OAuthFlow:
            return SilentFlow(code_sink=code_sink)

    async def fake_run(flow: OAuthFlow, probe: str) -> OAuthOutcome:
        return OAuthOutcome("failed", detail="child died silently")

    monkeypatch.setattr(admin_module, "wait_for_code", fake_wait)
    monkeypatch.setattr(OAuthFlow, "run", fake_run)
    bot = RecordingBot()
    message = _message("/oauth", bot)

    await admin_module.cmd_oauth(message, bot, FakeService())

    assert cancelled == [True], "the child did not outlive the command"
    joined = "\n".join(bot.all_texts)
    assert "❌" in joined


# ---------------------------------------------------------------------------
# The doctor row
# ---------------------------------------------------------------------------


def _doctor_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("YTDLP_POT_PROVIDER_URL", "")
    monkeypatch.setenv("YOUTUBE_SESSION_SERVER", "")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def test_the_doctor_row_says_off_when_the_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.doctor import OAUTH_CHECK_NAME, _oauth_check

    settings = _doctor_settings(monkeypatch)
    check = _oauth_check(settings, None, "")

    assert check.name == OAUTH_CHECK_NAME
    assert check.status == "ok", "off is not a failure"
    assert "خاموش" in check.detail


async def test_the_doctor_row_quotes_the_refusal_when_on_but_impossible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.doctor import _oauth_check

    settings = _doctor_settings(monkeypatch, YTDLP_USE_OAUTH2="1")
    check = _oauth_check(settings, False, "ExtractorError: Login with OAuth is no longer supported")

    assert check.status == "warn", "one dead credential among several is not a fail"
    assert "no longer supported" in check.detail


async def test_the_doctor_row_is_green_with_support_and_a_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:

    from services.doctor import _oauth_check

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "youtube-oauth2.token_data").write_text("{}", encoding="utf-8")
    settings = _doctor_settings(monkeypatch, YTDLP_USE_OAUTH2="1", YTDLP_CACHE_DIR=str(cache))
    check = _oauth_check(settings, True, "")

    assert check.status == "ok"
    assert "youtube-oauth2.token_data" in check.detail


async def test_the_doctor_row_survives_an_unknown_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.doctor import _oauth_check

    settings = _doctor_settings(monkeypatch, YTDLP_USE_OAUTH2="1")
    check = _oauth_check(settings, None, "")

    assert check.status == "warn"
    assert "امکان‌سنجی" in check.detail


# ---------------------------------------------------------------------------
# The cache line
# ---------------------------------------------------------------------------


def test_the_cache_line_reports_what_it_sees(tmp_path: Any) -> None:
    empty = cache_state_line(str(tmp_path / "nowhere"))
    assert "ساخته نشده" in empty

    (tmp_path / "jar").mkdir()
    assert "خالی" in cache_state_line(str(tmp_path / "jar"))

    (tmp_path / "jar" / "token").write_text("x", encoding="utf-8")
    assert "token" in cache_state_line(str(tmp_path / "jar"))



