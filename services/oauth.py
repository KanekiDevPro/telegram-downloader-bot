"""Interactive YouTube OAuth2 login — the Smart-TV device flow, driven from Telegram.

**Read this before turning the knob on.** yt-dlp gained OAuth2 in 2024.10.22
(``--username oauth2 --password ''``) and YouTube later revoked it: current core
raises *Login with OAuth is no longer supported* from ``_perform_login`` for any
``username.startswith('oauth')``, and the wiki says to use cookies instead. This
module therefore never *assumes* the flow works — it asks the installed yt-dlp
first ( :func:`oauth_supported`, by instantiating the extractor in a worker
thread and checking the only two outcomes the answer can take), and its Telegram
command reports the refusal instead of hanging on a code that will never be
issued. A plugin that revives the flow — installed into the plugin directory
mounted at ``/app/config/yt-dlp`` — is the intended way to turn it back on, and
needs no further code here: the child process, the stderr grammar and the cache
behaviour are all the plugin's contract too.

The flow itself is one child ``yt-dlp`` process: it prints the device code to
stderr and polls Google until the code is entered (or the code expires), writes
the token into its cache directory, and exits 0 — after which the *next*
extraction passes ``username: 'oauth2'`` and the cached token signs the request
with no interaction at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from yt_dlp.extractor.youtube import YoutubeIE

logger = logging.getLogger(__name__)

#: The device URL every shape of the prompt names. Shown to the admin as a
#: button; also the needle the stderr watcher looks for so the message can carry
#: a ``code`` it did not have to be told.
DEVICE_URL = "https://www.google.com/device"

#: The prompt grammar, in the shapes yt-dlp (and the obsolete plugin that
#: originated it) have printed: a URL, then the code, hyphen-separated. The
#: ``\\s*`` tolerates ``XXX-YYY-ZZZ`` on its own line or trailing the sentence.
_CODE_PATTERN = re.compile(
    r"([A-Z0-9]{3,6}(?:\s*[-–]\s*[A-Z0-9]{3,6}){1,2})"
)

#: The refusal text current core raises from ``_perform_login``. Searched for in
#: the probe, and — belt and braces — in the child's stderr: a core that gains
#: the message *after* the probe window (plugin load order) still reads as
#: unsupported rather than as a mysterious hang.
_OAUTH_REFUSAL = "Login with OAuth is no longer supported"

#: How long the child may run before it is abandoned. Google's own device code
#: expires after 30 minutes; one poll cycle runs ~5s, so the child normally
#: exits when the code is entered or expires — this is the outer bound only.
CHILD_TIMEOUT_S = 40.0 * 60.0

#: Seconds between the admin being told the flow is still waiting and how much
#: of the window is left. Three nudges: at start, mid-way, near expiry — enough
#: that a silent window never reads as a hung bot, not so many that the chat
#: becomes the progress bar.
_REMINDER_S = (0.0, 15.0 * 60.0, 25.0 * 60.0)


def _probe_in_thread() -> tuple[bool, str]:
    """The probe's blocking half: instantiate the extractor, read its refusal.

    ``_perform_login`` on current core raises :class:`ExtractorError` (with the
    refusal text); a build that still implements the flow *warns* ("Login with
    password is not supported" — the OAuth path returns before it). Both come
    back as facts rather than as a crashed process.
    """
    import contextlib
    import io

    buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
            instance = YoutubeIE()
            instance._perform_login("oauth2", "")
    except Exception as exc:  # noqa: BLE001 — the refusal *is* the expected answer
        text = f"{type(exc).__name__}: {exc}"
        # The known refusal is the one answer that means "cannot": anything else
        # (a network error, a plugin's own complaint) implies an implementation
        # that genuinely *tried* the flow, which is the caller's business to run.
        return _OAUTH_REFUSAL not in text, text
    # No exception: the build has a real login path (the old plugin's behaviour).
    return True, "no refusal raised"


async def probe_oauth_support() -> tuple[bool, str]:
    """Does the installed yt-dlp still implement the OAuth2 device flow?

    Runs :func:`_probe_in_thread` off the event loop (the constructor reads
    plugin directories and can touch the disk). The second element is the
    evidence: the refusal message when the answer is no, so ``/oauth`` can quote
    it instead of a bare "not supported".
    """
    return await asyncio.to_thread(_probe_in_thread)


def probe_oauth_support_sync() -> bool:
    """The blocking probe, for callers already off the event loop.

    :meth:`ExtractorService._base_opts` runs in a worker thread by design (the
    whole point of the executor wrapper), so awaiting ``to_thread`` there is
    both illegal and unnecessary — this is the same measurement, called
    directly. First element of the tuple only; the refusal text is for the
    report, not the request path.
    """
    supported, _evidence = _probe_in_thread()
    return supported


@dataclass(frozen=True)
class OAuthOutcome:
    """How one device-flow attempt ended — and what to tell the admin."""

    #: ``success``: the child exited 0 (the token is in the cache). ``unsupported``:
    #: the probe already knew, or the child died with the known refusal.
    #: ``expired``: the code was never entered. ``failed``: any other exit.
    status: str
    #: The code captured from stderr, when one was ever issued (the admin has
    #: already been shown it — the outcome repeats it so the success message can
    #: confirm *which* login landed).
    code: str | None = None
    detail: str = ""


class OAuthFlow:
    """One interactive ``yt-dlp --username oauth2`` child process.

    Created per ``/oauth`` run; two runs at once would race over the same cache
    entry, so the service keeps at most one (a second request is answered with
    "already running", not queued — a login that needs the operator's *attention*
    cannot wait in a line behind another one that may be abandoned).
    """

    def __init__(
        self,
        *,
        cache_dir: str = "",
        proxy: str = "",
        code_sink: "asyncio.Queue[str] | None" = None,
        python_executable: str = sys.executable,
    ) -> None:
        self._cache_dir = cache_dir.strip()
        self._proxy = proxy.strip()
        #: Codes captured from stderr land here as they are seen; the handler
        #: reads with a timeout so a silent child cannot block the admin chat.
        self.code_sink = code_sink
        self._python = python_executable
        self._process: asyncio.subprocess.Process | None = None
        self.code: str | None = None

    def _argv(self, probe_url: str) -> list[str]:
        """The exact child command: module invocation, no shell, no quoting."""
        argv = [self._python, "-m", "yt_dlp", "--username", "oauth2", "--password", ""]
        if self._cache_dir:
            argv += ["--cache-dir", self._cache_dir]
        if self._proxy:
            argv += ["--proxy", self._proxy]
        argv += ["--dump-json", probe_url]
        return argv

    @staticmethod
    def _extract_code(text: str) -> str | None:
        """The device code in a stderr line, if there is one.

        The code the admin copies must look exactly like what Google's page
        expects — ``XXX-YYY-ZZZ``, hyphens in — so whitespace is stripped and an
        en-dash a terminal mangled in is converted *back* to a hyphen rather
        than removed.
        """
        match = _CODE_PATTERN.search(text)
        if match is None:
            return None
        code = re.sub(r"\s+", "", match.group(1))
        return code.replace("–", "-").upper()

    async def run(self, probe_url: str) -> OAuthOutcome:
        """Start the child, watch its stderr for the code, and await its end."""
        argv = self._argv(probe_url)
        logger.info("OAuth device flow starting: %s", " ".join(argv))
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return OAuthOutcome("failed", detail=f"{type(exc).__name__}: {exc}")

        assert self._process.stderr is not None
        stderr_lines: list[str] = []
        reader = asyncio.create_task(self._read_stderr(self._process.stderr, stderr_lines))

        try:
            return_code = await asyncio.wait_for(self._process.wait(), timeout=CHILD_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.cancel()
            await reader
            return OAuthOutcome("expired", code=self.code, detail="the device code was not entered in time")
        await reader

        stderr_text = "\n".join(stderr_lines)
        if _OAUTH_REFUSAL in stderr_text:
            # A build whose refusal appears only once the extractor is fully
            # loaded (plugin ordering) — the probe could not have seen it.
            return OAuthOutcome("unsupported", code=self.code, detail=stderr_text.strip()[:400])
        if return_code == 0:
            return OAuthOutcome("success", code=self.code)
        return OAuthOutcome(
            "failed", code=self.code, detail=stderr_text.strip()[-400:] or f"exit code {return_code}"
        )

    async def _read_stderr(
        self,
        stream: asyncio.StreamReader,
        lines: list[str],
    ) -> None:
        """Every stderr line, remembered — and any device code pushed live."""
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            lines.append(text)
            logger.debug("oauth child: %s", text)
            if "google.com/device" in text and self.code is None:
                if found := self._extract_code(text):
                    self.code = found
                    if self.code_sink is not None:
                        await self.code_sink.put(found)

    async def cancel(self) -> None:
        """Stop the child — SIGTERM, then SIGKILL. Never raises."""
        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass


class OAuthService:
    """The one-flow-at-a-time owner of the device login.

    Also owns the *capability* answer: probed once, before any Telegram command
    can spend an admin's time on a flow that cannot start.
    """

    def __init__(self, *, cache_dir: str = "", proxy: str = "") -> None:
        self._cache_dir = cache_dir.strip()
        self._proxy = proxy.strip()
        self._flow: OAuthFlow | None = None
        self._lock = asyncio.Lock()
        self._support: tuple[bool, str] | None = None

    async def supported(self) -> tuple[bool, str]:
        """``(supported, evidence)`` — probed on first use, then remembered."""
        if self._support is None:
            self._support = await probe_oauth_support()
        return self._support

    @property
    def running(self) -> bool:
        return self._flow is not None

    async def start(
        self, probe_url: str, *, code_sink: "asyncio.Queue[str] | None" = None
    ) -> OAuthFlow:
        """Begin one device flow, or raise :class:`FlowRunning` if one is live."""
        async with self._lock:
            if self._flow is not None:
                raise FlowRunning()
            flow = OAuthFlow(
                cache_dir=self._cache_dir, proxy=self._proxy, code_sink=code_sink
            )
            self._flow = flow
            return flow

    async def finish(self, flow: OAuthFlow) -> None:
        """Mark the flow over — success, refusal or abandonment all land here."""
        async with self._lock:
            if self._flow is flow:
                self._flow = None

    async def cancel(self) -> None:
        """Used at shutdown: no child may outlive the bot that started it."""
        async with self._lock:
            flow, self._flow = self._flow, None
        if flow is not None:
            await flow.cancel()


class FlowRunning(Exception):
    """A second ``/oauth`` while one is live — answered, not queued."""


async def wait_for_code(sink: "asyncio.Queue[str]", timeout_s: float) -> str | None:
    """Read one code from the sink, or give up after ``timeout_s``.

    The queue is drained afterwards so a late code cannot leak into a *future*
    flow's sink (queues are per-flow, but the handler may create its reader
    before the child starts writing).
    """
    try:
        return await asyncio.wait_for(sink.get(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


def cache_state_line(cache_dir: str) -> str:
    """One line about the cache directory: exists, empty, or not created yet.

    The OAuth token (under a plugin) lives in yt-dlp's cache, so "is anything in
    there" is the closest a dry question gets to "am I logged in".
    """
    path = Path(cache_dir) if cache_dir else Path()
    try:
        if not path.exists():
            return f"{path} — ساخته نشده هنوز (اولین لاگین می‌سازدش)"
        entries = [entry for entry in path.iterdir()]
        if not entries:
            return f"{path} — خالی"
        newest = max(entries, key=lambda entry: entry.stat().st_mtime)
        return f"{path} — {len(entries)} فایل، آخرین تغییر {newest.name}"
    except OSError as exc:
        return f"{path} — خوانده نشد ({exc})"
