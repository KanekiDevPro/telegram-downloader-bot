"""Core extractor service: wraps blocking yt-dlp calls for async use.

Everything that touches yt-dlp runs in a worker thread (``asyncio.to_thread``)
so the event loop never blocks — the bot stays responsive under load.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import importlib.metadata
import importlib.util
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence, TypeVar
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.cookies import SUPPORTED_BROWSERS, SUPPORTED_KEYRINGS, load_cookies
from yt_dlp.utils import DownloadError

from core.utils import MediaFormat, normalize_quality

ProgressHook = Callable[[dict[str, Any]], None]
T = TypeVar("T")

#: Failure codes worth another attempt. YouTube's stale-session error ("The page
#: needs to be reloaded") is routinely transient — a rotated visitor binding — and
#: the retry costs one round trip instead of a user-visible failure. Blocks are
#: deliberately *not* here: retrying a flagged IP just wastes the user's time.
RETRYABLE_EXTRACTION_CODES: frozenset[str] = frozenset({"SESSION_STALE"})

#: What yt-dlp's ``--force-ipv4`` writes into ``source_address``. Not a local
#: interface choice: yt-dlp's own socket layer reads the *family* of this address
#: and filters the resolved candidates down to it (``yt_dlp/networking/_helper.py``),
#: so IPv6 is never attempted — which is the point, with a WARP exit.
IPV4_ANY = "0.0.0.0"

#: The credentials yt-dlp's OAuth2 flow is keyed on. ``oauth2`` is what the
#: extractor checks (``username.startswith('oauth')``), and the empty password is
#: the contract from its own docs — the device flow authenticates the device, not
#: an account. Kept here so the extractor and the interactive flow in
#: ``services/oauth.py`` cannot drift apart.
OAUTH2_USERNAME = "oauth2"
OAUTH2_PASSWORD = ""

# ---------------------------------------------------------------------------
# Format selection (Telegram-optimised output)
# ---------------------------------------------------------------------------
# Preference order:
#   1. H.265/HEVC video (``hev1``/``hvc1``) + m4a audio — best quality per byte
#      and natively playable in Telegram clients.
#   2. H.264/AVC (``avc1``) + m4a — maximum compatibility fallback.
#   3. Whatever the site considers best, then the single-file best.
# Streams are merged into MP4 (MKV only if a site offers nothing MP4-compatible)
# because Telegram handles MP4 with inline streaming best.
#: ``hev1``/``hvc1`` are the two HEVC codec tags YouTube ships; ``avc1``/``h264``
#: the H.264 ones. Each filter uses its own bracket — yt-dlp ANDs them, whereas
#: combining conditions inside one pair of brackets is a syntax error.
_HEVC_VIDEO = "bestvideo[vcodec~='^(hev|hvc)'][ext=mp4]"
_AVC_VIDEO = "bestvideo[vcodec~='^(avc|h264)'][ext=mp4]"

VIDEO_FORMAT_SELECTOR = "/".join(
    (
        f"{_HEVC_VIDEO}+bestaudio[ext=m4a]",
        f"{_HEVC_VIDEO}+bestaudio",
        f"{_AVC_VIDEO}+bestaudio[ext=m4a]",
        f"{_AVC_VIDEO}+bestaudio",
        "bestvideo+bestaudio",
        "best",
    )
)
AUDIO_FORMAT_SELECTOR = "bestaudio[ext=m4a]/bestaudio/best"
MERGE_OUTPUT_FORMAT = "mp4/mkv"


def _video_selector(height: int) -> str:
    """The video chain with a height *ceiling* on every step.

    ``[height<=N]`` and never ``[height=N]``: a request for 1080p on a video that
    only exists in 480p must return the 480p file, not nothing. The trailing
    ``best``/``best[height<=N]`` pair is the other half of that promise — a site that
    reports no height at all would otherwise match no filter and fail with "requested
    format is not available", which is a *worse* answer than a file that is slightly
    bigger than asked for.
    """
    ceiling = f"[height<={height}]"
    return "/".join(
        (
            f"{_HEVC_VIDEO}{ceiling}+bestaudio[ext=m4a]",
            f"{_HEVC_VIDEO}{ceiling}+bestaudio",
            f"{_AVC_VIDEO}{ceiling}+bestaudio[ext=m4a]",
            f"{_AVC_VIDEO}{ceiling}+bestaudio",
            f"bestvideo{ceiling}+bestaudio",
            f"best{ceiling}",
            "best",
        )
    )


def format_selector(media_format: MediaFormat, quality: object = "") -> str:
    """yt-dlp ``-f`` value for the requested media type and quality tier.

    ``quality`` is a :data:`core.utils.Quality`: a height ceiling for video (``best``
    means the existing no-ceiling chain), and ``mp3``/``m4a`` for audio — where m4a
    is the untouched stream, so the *post-processor* is what differs, not the
    selector (see ``_download_attempt``).
    """
    if media_format == "audio":
        return AUDIO_FORMAT_SELECTOR
    tier = normalize_quality(quality, media_format)
    if tier == "best":
        return VIDEO_FORMAT_SELECTOR
    return _video_selector(int(tier))


#: yt-dlp's own spelling of ``--cookies-from-browser``: BROWSER[+KEYRING][:PROFILE][::CONTAINER].
#: Copied from its CLI parser so a value accepted here is accepted there too.
_BROWSER_SPEC_RE = re.compile(
    r"""(?x)
        (?P<name>[^+:]+)
        (?:\s*\+\s*(?P<keyring>[^:]+))?
        (?:\s*:\s*(?!:)(?P<profile>.+?))?
        (?:\s*::\s*(?P<container>.+))?
    """
)


class BrowserSpecError(ValueError):
    """Raised for a ``COOKIES_FROM_BROWSER`` value yt-dlp would reject."""


def parse_browser_spec(spec: str) -> tuple[str, str | None, str | None, str | None]:
    """Parse ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` into yt-dlp's 4-tuple.

    yt-dlp takes ``cookiesfrombrowser`` as a tuple and raises ``TypeError`` for a
    bare string, so the CLI spelling has to be converted before it reaches the
    API. Unknown browsers/keyrings are rejected here rather than mid-download.
    """
    match = _BROWSER_SPEC_RE.fullmatch(spec.strip())
    if match is None:
        raise BrowserSpecError(f"invalid cookies-from-browser value: {spec!r}")
    browser, keyring, profile, container = match.group("name", "keyring", "profile", "container")
    browser = browser.lower()
    if browser not in SUPPORTED_BROWSERS:
        supported = ", ".join(sorted(SUPPORTED_BROWSERS))
        raise BrowserSpecError(f"unsupported browser {browser!r} (supported: {supported})")
    if keyring is not None:
        keyring = keyring.upper()  # yt-dlp spellings are upper case (GNOMEKEYRING)
        if keyring not in SUPPORTED_KEYRINGS:
            supported = ", ".join(sorted(SUPPORTED_KEYRINGS))
            raise BrowserSpecError(f"unsupported keyring {keyring!r} (supported: {supported})")
    return browser, profile, keyring, container


#: Where each browser keeps its profile, relative to the home directory, per
#: platform family. Only used to answer one question *before* an export is worth
#: attempting: is there a browser on this machine at all? A container has none,
#: and spending an attempt (plus an admin alert) to learn that is a waste. A
#: layout we do not know means "try anyway" — a wrong guess must never block a
#: setup that actually works.
_BROWSER_PROFILE_DIRS: dict[str, dict[str, str]] = {
    "chrome": {
        "windows": "AppData/Local/Google/Chrome/User Data",
        "macos": "Library/Application Support/Google/Chrome",
        "linux": ".config/google-chrome",
    },
    "chromium": {
        "windows": "AppData/Local/Chromium/User Data",
        "macos": "Library/Application Support/Chromium",
        "linux": ".config/chromium",
    },
    "edge": {
        "windows": "AppData/Local/Microsoft/Edge/User Data",
        "macos": "Library/Application Support/Microsoft Edge",
        "linux": ".config/microsoft-edge",
    },
    "brave": {
        "windows": "AppData/Local/BraveSoftware/Brave-Browser/User Data",
        "macos": "Library/Application Support/BraveSoftware/Brave-Browser",
        "linux": ".config/BraveSoftware/Brave-Browser",
    },
    "vivaldi": {
        "windows": "AppData/Local/Vivaldi/User Data",
        "macos": "Library/Application Support/Vivaldi",
        "linux": ".config/vivaldi",
    },
    "opera": {
        "windows": "AppData/Roaming/Opera Software/Opera Stable",
        "macos": "Library/Application Support/com.operasoftware.Opera",
        "linux": ".config/opera",
    },
    "whale": {
        "windows": "AppData/Local/Naver/Whale/User Data",
        "macos": "Library/Application Support/Naver/Whale",
        "linux": ".config/naver-whale",
    },
    "firefox": {
        "windows": "AppData/Roaming/Mozilla/Firefox",
        "macos": "Library/Application Support/Firefox",
        "linux": ".mozilla/firefox",
    },
    # Safari is macOS-only and reads through the system cookie store, so there is
    # no per-profile path to check: unknown, which means "just try" (below).
}

#: Inside a Chromium profile directory. ``Network/`` is what a modern browser
#: writes; the bare name is what older versions left behind.
_CHROMIUM_COOKIE_TAILS: tuple[str, ...] = ("Network/Cookies", "Cookies")


def _platform_family() -> str:
    """Which of the layouts above applies here."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def browser_profile_paths(spec: str) -> tuple[Path, ...]:
    """Where this profile's cookie store would be on *this* machine.

    Empty means "cannot see one", not "does not exist": the check below only
    draws conclusions where the layout is known.
    """
    browser, profile, _, _ = parse_browser_spec(spec)
    base = _BROWSER_PROFILE_DIRS.get(browser, {}).get(_platform_family())
    if base is None:
        return ()
    root = Path.home() / base
    if browser == "firefox":
        # Firefox profile directories are randomly named; the spec names the
        # profile, and profiles.ini is what yt-dlp reads to find its directory.
        return (root / "profiles.ini", root)
    name = profile or "Default"
    return tuple(root / name / tail for tail in _CHROMIUM_COOKIE_TAILS)


def browser_profile_reachable(spec: str) -> bool | None:
    """Is there a profile here to read? ``True`` / ``False`` / ``None`` (unknown).

    ``False`` is only returned when the browser's layout is known *and* nothing is
    where it would be — the plain-container case, where the export could only end
    in "no browser here". ``None`` means the caller should just try.
    """
    try:
        paths = browser_profile_paths(spec)
    except BrowserSpecError:
        return None
    if not paths:
        return None
    return any(path.exists() for path in paths)


def browser_local_state(spec: str) -> Path | None:
    """The Chromium ``Local State`` file for this browser, when it has one."""
    browser, _, _, _ = parse_browser_spec(spec)
    if browser == "firefox":
        return None
    base = _BROWSER_PROFILE_DIRS.get(browser, {}).get(_platform_family())
    return Path.home() / base / "Local State" if base else None


def app_bound_encryption_active(spec: str) -> bool:
    """Whether Windows App-Bound Encryption stands between us and this profile.

    Chrome 127+ (Edge, Brave and the rest followed) encrypt the key that protects
    the cookie database with the *browser's own identity*, so nothing outside the
    browser can decrypt it — not yt-dlp, not ``--cookies-from-browser``. The key's
    presence in ``Local State`` says so before an export is attempted, which turns
    an unexplainable "failed to load cookies" into the two ways that do work.
    """
    if not sys.platform.startswith("win"):
        return False  # the scheme is Windows-only; Linux/macOS unlock normally
    state_file = browser_local_state(spec)
    if state_file is None:
        return False
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(state.get("os_crypt", {}).get("app_bound_encrypted_key"))


#: Executables yt-dlp can drive, in its own priority order.
JS_RUNTIME_EXECUTABLES: dict[str, str] = {
    "deno": "deno",
    "node": "node",
    "bun": "bun",
    "quickjs": "qjs",  # QuickJS / QuickJS-ng
}


def detect_js_runtimes(spec: str = "auto") -> dict[str, dict[str, str]]:
    """Resolve yt-dlp's ``js_runtimes`` option.

    yt-dlp only enables ``deno`` by default and otherwise warns that "YouTube
    extraction without a JS runtime has been deprecated, and some formats may be
    missing" — so any runtime that is actually installed is enabled here.

    ``spec`` is ``auto`` (default), a runtime name, ``name:/path/to/binary``, or
    ``none``/empty to leave yt-dlp on its own default.
    """
    spec = spec.strip()
    if spec.lower() in {"", "none", "off", "0"}:
        return {}
    if spec.lower() != "auto":
        name, _, path = spec.partition(":")
        name = name.strip().lower()
        executable = path.strip() or JS_RUNTIME_EXECUTABLES.get(name, name)
        return {name: {"path": executable}}
    for name, executable in JS_RUNTIME_EXECUTABLES.items():
        found = shutil.which(executable)
        if found:
            return {name: {"path": found}}
    return {}


#: yt-dlp plugin package that turns the provider URL into actual PO tokens.
POT_PLUGIN_MODULE = "yt_dlp_plugins.extractor.getpot_bgutil_http"
#: The distribution that installs it — the version is read from *here*, deliberately.
POT_PLUGIN_DISTRIBUTION = "bgutil-ytdlp-pot-provider"


def pot_plugin_installed() -> bool:
    """Is the bgutil PO-token provider plugin available to yt-dlp?

    The provider URL only has an effect through that plugin, so a configured URL
    without the plugin is silently inert — worth warning about at startup.
    """
    try:
        return importlib.util.find_spec(POT_PLUGIN_MODULE) is not None
    except (ImportError, ValueError):
        return False


def pot_plugin_version() -> str | None:
    """The installed plugin's version, as it reports it to the provider server.

    Worth knowing beyond curiosity: the plugin *rejects* a server whose major
    version differs from its own (a PO token from a different protocol generation
    would be unusable), so a drifting `:latest` image silently costs every
    download its token. The doctor compares the two.

    Read from the installed *distribution*, never by importing the module: the
    plugin registers itself with yt-dlp's provider registry as an import side
    effect, and a registration of ours would make yt-dlp's own plugin loader fail
    with "already registered" — an error about a plugin that is perfectly fine,
    inside the same process that is about to download.
    """
    try:
        return importlib.metadata.version(POT_PLUGIN_DISTRIBUTION)
    except Exception:  # noqa: BLE001 — not installed / broken metadata ⇒ no version
        return None


def browser_cookie_jar_is_usable(spec: str) -> bool:
    """True when yt-dlp can actually read cookies from that browser profile.

    Chrome/Firefox profiles are frequently unreachable from inside a container
    (a different OS user, a different keyring) — and yt-dlp turns a failed
    extraction into ``CookieLoadError`` for *every* download. Probe once and
    ignore a spec that cannot work instead of breaking the whole bot.
    """
    try:
        browser, profile, keyring, container = parse_browser_spec(spec)
    except BrowserSpecError as exc:
        logger.warning("COOKIES_FROM_BROWSER ignored: %s", exc)
        return False
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            # Same call yt-dlp makes for --cookies-from-browser, so the probe
            # cannot pass while a real download would fail.
            jar = load_cookies(None, (browser, profile, keyring, container), ydl)
    except Exception as exc:  # noqa: BLE001 — any failure means "not usable here"
        logger.warning(
            "COOKIES_FROM_BROWSER=%s could not be read here (%s: %s) — ignoring it. "
            "Browser profiles are usually not reachable from inside a container; "
            "run scripts/export_cookies.py on the machine with the browser instead.",
            spec,
            type(exc).__name__,
            exc,
        )
        return False
    count = len(jar) if jar is not None else 0
    if count == 0:
        logger.warning("COOKIES_FROM_BROWSER=%s produced no cookies — ignoring it.", spec)
        return False
    return True


logger = logging.getLogger(__name__)


def cookie_jar_is_usable(path: Path | None) -> bool:
    """True when yt-dlp would actually load cookies from ``path``.

    yt-dlp raises ``DownloadError`` ("does not look like a Netscape format
    cookies file") for an empty or truncated jar — which would then fail *every*
    download. A file that a browser extension didn't fully write, or a stray
    ``touch cookies.txt``, must therefore be ignored, not passed through.

    A *directory* is the same verdict for a different reason: Docker creates one
    where a bind-mounted *file* was expected (and an older revision of
    docker-compose.yml mounted the jar that way), and paths that do not exist are
    not jars either.
    """
    if path is None or not path.is_file() or path.stat().st_size == 0:
        return False
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return False
    return len(jar) > 0


#: Cookies yt-dlp itself demands before it treats a YouTube session as signed in.
#: Mirrors ``YoutubeBaseIE._has_auth_cookies``: ``LOGIN_INFO`` must be present
#: *and* one cookie of the SAPISID family, otherwise every request is sent
#: anonymously — which is what makes a flagged IP answer "Sign in to confirm
#: you're not a bot" no matter how valid the rest of the jar looks.
YOUTUBE_LOGIN_COOKIE = "LOGIN_INFO"
YOUTUBE_SAPISID_COOKIES: tuple[str, ...] = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")


def read_netscape_cookie_rows(path: Path | None) -> list[list[str]]:
    """Cookie rows in a Netscape jar, ``#HttpOnly_`` rows included.

    ``http.cookiejar`` strips the ``#HttpOnly_`` prefix while loading, but the
    cookies that carry a YouTube login (``LOGIN_INFO``, the SAPISID family) are
    exactly the HttpOnly ones — and an exporter that silently drops HttpOnly
    rows removes the whole login while still producing a file that loads fine.
    Reading the rows directly keeps them visible. Header and comment lines are
    skipped, expired rows are not.
    """
    if path is None or not path.is_file():
        return []
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        row = line
        if row.startswith("#HttpOnly_"):
            row = row[len("#HttpOnly_") :]
        elif row.startswith("#") or not row.strip():
            continue
        fields = row.split("\t")
        if len(fields) >= 7:
            rows.append(fields)
    return rows


def read_netscape_cookie_names(path: Path | None) -> frozenset[str]:
    """Cookie names in a jar; see :func:`read_netscape_cookie_rows`."""
    return frozenset(fields[5] for fields in read_netscape_cookie_rows(path))


def count_netscape_cookies(path: Path | None) -> int:
    """How many cookie rows a jar holds (a size the operator can recognise)."""
    return len(read_netscape_cookie_rows(path))


def missing_youtube_login_cookies(path: Path | None) -> tuple[str, ...]:
    """Which YouTube login cookies a jar lacks (empty tuple when complete)."""
    names = read_netscape_cookie_names(path)
    missing: list[str] = []
    if YOUTUBE_LOGIN_COOKIE not in names:
        missing.append(YOUTUBE_LOGIN_COOKIE)
    if not names.intersection(YOUTUBE_SAPISID_COOKIES):
        missing.append(" / ".join(YOUTUBE_SAPISID_COOKIES))
    return tuple(missing)


def youtube_client_facts(
    clients: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """``(unknown, token_required, token_free)`` for a configured client list.

    Read from the *installed* yt-dlp, because that is what decides it: every client
    in its ``INNERTUBE_CLIENTS`` table carries a GVS PO-token policy, and the clients
    that require one are precisely the ones a "spoof another device" list is usually
    trying to escape (in 2026.08.19 that is ``android``, ``android_vr``, ``ios``,
    ``web``, ``web_safari``, ``web_music``, ``web_creator``, ``mweb``, ``tv_simply``).
    A name yt-dlp does not know is skipped by yt-dlp itself with a warning, so
    reporting it beats applying it.

    Never raises: a yt-dlp that moved its table is a report that says "cannot say",
    not a bot that stops diagnosing YouTube.
    """
    if not clients:
        return (), (), ()
    try:
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
    except Exception:  # noqa: BLE001 — an import that moved is not a crash
        return (), (), ()
    unknown: list[str] = []
    required: list[str] = []
    free: list[str] = []
    for client in clients:
        config = INNERTUBE_CLIENTS.get(client)
        # Names starting with ``_`` exist for yt-dlp's internal use and cannot be
        # requested by a user, so they count as unknown in a configured list.
        if not isinstance(config, dict) or client.startswith("_"):
            unknown.append(client)
            continue
        policies = config.get("GVS_PO_TOKEN_POLICY") or {}
        needs_token = any(getattr(policy, "required", False) for policy in policies.values())
        (required if needs_token else free).append(client)
    return tuple(unknown), tuple(required), tuple(free)


def youtube_login_hint(path: Path | None) -> str | None:
    """Advice for a jar that carries cookies but cannot sign YouTube in.

    Returns ``None`` when there is no jar, the jar is unusable for other
    reasons, or the login is complete — this only speaks about the one failure
    that looks like an IP ban but is not.
    """
    if not cookie_jar_is_usable(path):
        return None
    missing = missing_youtube_login_cookies(path)
    if not missing:
        return None
    return (
        f"COOKIE_FILE={path} loads, but has no YouTube login: {', '.join(missing)} "
        "missing. yt-dlp only signs in when LOGIN_INFO plus a SAPISID cookie are "
        "present, so every request goes out anonymous and a flagged IP answers "
        "'Sign in to confirm you're not a bot'. Re-export the jar while signed in "
        "to YouTube and keep HttpOnly rows (scripts/export_cookies.py, or a "
        "'Get cookies.txt' extension configured to include HttpOnly) — a PO token "
        "provider or a proxy will not substitute for the login."
    )


@dataclass(frozen=True)
class MountInfo:
    """Which mount a path lives on, and whether that mount refuses writes."""

    point: str
    root: str
    filesystem: str
    source: str
    read_only: bool


def _unescape_mount_field(value: str) -> str:
    """``/proc/self/mountinfo`` escapes spaces and newlines as octal (``\\040``)."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def parse_mountinfo(text: str) -> list[MountInfo]:
    """Parse ``/proc/self/mountinfo`` into mount entries.

    Field layout: ``id parent major:minor root point options [optional…] - fstype
    source super-options``. Split out from the lookup below so the parser can be
    tested with real kernel text instead of whatever mounts the test host has.
    """
    entries: list[MountInfo] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6 or len(fields) < separator + 3:
            continue
        # A read-only mount is flagged either per-mount (``ro``) or on the
        # filesystem itself (super options) — a read-only bind mount uses the
        # first, a read-only disk the second.
        options = fields[5].split(",")
        super_options = fields[separator + 3].split(",") if len(fields) > separator + 3 else []
        entries.append(
            MountInfo(
                point=_unescape_mount_field(fields[4]),
                root=_unescape_mount_field(fields[3]),
                filesystem=fields[separator + 1],
                source=_unescape_mount_field(fields[separator + 2]),
                read_only="ro" in options or "ro" in super_options,
            )
        )
    return entries


def select_mount(entries: list[MountInfo], target: str) -> MountInfo | None:
    """The most specific mount containing ``target`` (an absolute path).

    Longest mount point wins: ``/cookies`` beats ``/`` even though both contain
    the path. Split out so the choice can be tested without real mounts.
    """
    best: MountInfo | None = None
    for entry in entries:
        if target == entry.point or target.startswith(entry.point.rstrip("/") + "/"):
            if best is None or len(entry.point) > len(best.point):
                best = entry
    return best


def mount_info_for(path: Path) -> MountInfo | None:
    """The mount containing ``path``, or ``None`` off Linux (a host machine)."""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        target = str(path.resolve())
    except OSError:
        return None
    return select_mount(parse_mountinfo(text), target)


CookieJarKind = Literal["ok", "missing", "directory", "unusable"]


@dataclass(frozen=True)
class CookieJarState:
    """Everything the bot can say about its cookie jar without guessing.

    Built for the doctor, which has to answer three operator questions at once:
    is the jar there, where does it come from (a read-only mount?), and is the
    running bot already using the newest export.
    """

    path: Path | None
    kind: CookieJarKind
    size_bytes: int | None
    #: When the *human* wrote the jar (the export itself).
    exported_at: float | None
    mount: MountInfo | None
    writable: bool
    cookie_count: int
    missing_login: tuple[str, ...]
    #: The jar yt-dlp is handed (a writable copy), and when it was written.
    copy_path: Path | None
    copy_refreshed_at: float | None
    #: ``True`` = the copy was made from the jar on disk, ``False`` = a newer
    #: export is waiting, ``None`` = this process never loaded the jar (a CLI
    #: diagnosis, where the next download does it).
    in_sync: bool | None


class ExtractionError(Exception):
    """User-facing extraction failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _map_download_error(exc: DownloadError) -> ExtractionError:
    """Translate raw yt-dlp error text into a stable, user-friendly error."""
    text = str(exc)
    lowered = text.lower()
    mapping: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (
            # yt-dlp's policy list, not something it could recover from: the site
            # sits in its ``KnownDRMIE`` set, so *no* jar, token or proxy changes
            # the answer there — only a different engine can.
            ("known to use drm protection", "drm protected", "protected by drm"),
            DRM_PROTECTED_CODE,
            "این محتوا با DRM محافظت می‌شود و موتور استخراج از دانلودش پشتیبانی نمی‌کند.",
        ),
        (
            # The primary engine serves *video*: a post that only carries photos ends
            # here (twitter's "No video could be found in this tweet", reddit's "No
            # media found", bluesky/tumblr's "…in this post"). Nothing was refused —
            # the images are simply not its business, and they are the other
            # engine's.
            ("no video could be found", "no media found"),
            IMAGE_ONLY,
            "این لینک ویدیوی قابل دانلود ندارد (پست عکسی یا بدون رسانه).",
        ),
        (
            ("unsupported url", "is not a valid url", "no supporting extractor"),
            "UNSUPPORTED_URL",
            "این لینک توسط موتور استخراج پشتیبانی نمی‌شود.",
        ),
        (
            ("private video", "video unavailable", "this video is unavailable", "removed"),
            "PRIVATE_VIDEO",
            "ویدیو در دسترس نیست (خصوصی یا حذف‌شده).",
        ),
        (
            ("age-restricted", "this video is age-restricted"),
            "AGE_RESTRICTED",
            "ویدیو محدودیت سنی دارد و نیاز به تأیید حساب دارد.",
        ),
        (
            ("not available in your country", "geo-restricted", "geo restricted"),
            "GEO_RESTRICTED",
            "این محتوا در منطقه شما در دسترس نیست.",
        ),
        (
            ("live event", "is live", "live stream"),
            "LIVE_STREAM",
            "پخش زنده قابل دانلود نیست.",
        ),
        (
            ("the page needs to be reloaded",),
            "SESSION_STALE",
            "یوتیوب این درخواست را نپذیرفت (سشن کهنه است)؛ چند لحظه بعد دوباره تلاش کنید. "
            "اگر تکرار شد کوکی تازه و PO token provider لازم است.",
        ),
        (
            ("http error 403", "http error 429", "temporary ban", "requested format is not available",
             "sign in to confirm"),
            "EXTRACTOR_BLOCKED",
            "سایت مبدأ دانلود را مسدود کرد؛ کمی بعد دوباره تلاش کنید.",
        ),
        (
            ("timed out", "timeout", "connection reset"),
            "TIMEOUT",
            "ارتباط با سرور مبدأ قطع شد؛ دوباره تلاش کنید.",
        ),
    )
    for needles, code, msg in mapping:
        if any(needle in lowered for needle in needles):
            return ExtractionError(code, msg)
    return ExtractionError("GENERAL", f"خطای غیرمنتظره در استخراج: {text[:300]}")


#: Failures that can only mean the site refused to serve us.
BLOCK_EXTRACTION_CODES: frozenset[str] = frozenset({"EXTRACTOR_BLOCKED", "SESSION_STALE"})

#: Refusals a *search* is retried without the cookie jar for. Both are, in yt-dlp's
#: own words for the two, "retry without the cookies" cases: a session the site has
#: already rotated, or a request the site treats as a bot — and a jar that is in the
#: request is a known trigger for both. Only searches take that second opinion; see
#: ``_search_attempt`` for why a download deliberately does not.
_ANONYMOUS_RETRY_CODES: frozenset[str] = frozenset({"SESSION_STALE", "EXTRACTOR_BLOCKED"})

#: The link carries no *video* for the primary engine to fetch — an image post, or
#: a post with no media at all. Not a block (nothing was refused us) and not a dead
#: end either: photos are exactly what the fallback engine can serve.
IMAGE_ONLY = "IMAGE_ONLY"

#: A refusal by *policy* rather than by our address or our jar: yt-dlp answers
#: these sites with "known to use DRM protection" and never even tries, so nothing
#: on our side can change it. Deliberately outside ``BLOCK_EXTRACTION_CODES`` — a
#: DRM site is not a block, it is the site's own nature, which is exactly where
#: the digest's ``site`` bucket belongs (``classify_block`` says so itself).
DRM_PROTECTED_CODE = "DRM_PROTECTED"

#: YouTube (and its short-link/embed hosts) is the one platform whose refusal
#: carries a cookie meaning: a request without ``LOGIN_INFO`` is an unknown
#: visitor. Elsewhere a block is the site's own doing (rate limits, region).
YOUTUBE_HOSTS: tuple[str, ...] = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def url_host(url: str) -> str:
    """The host of a link, lowercased, without port or credentials (``?`` if none).

    Credentials are stripped *before* the port: ``user:pass@host:443`` has a colon in
    the credentials, and splitting on the first one would answer ``user`` — a value
    no URL ever has as its host.
    """
    netloc = urlparse(url).netloc.lower()
    host = netloc.rpartition("@")[2].partition(":")[0]
    return host or "?"


def is_youtube_url(url: str) -> bool:
    """Whether a link is served by YouTube, subdomains (``www.``/``m.``) included."""
    host = url_host(url)
    return any(host == known or host.endswith(f".{known}") for known in YOUTUBE_HOSTS)


def login_looking_block(
    error: ExtractionError, url: str, cookie_file: Path | None
) -> bool:
    """Is this refusal explained by *our* jar rather than by the site or the IP?

    Three audiences need the same answer: the user (whose link just failed), the
    admins (who can fix it) and the log (where a bare block always reads as an IP
    ban — the misdiagnosis this project keeps having to undo). Two shapes qualify:
    no usable jar at all, so every request is anonymous; or a jar that cannot sign
    YouTube in, on a YouTube link. A complete login plus a block is *not* this —
    that one is worth a proxy.
    """
    if error.code not in BLOCK_EXTRACTION_CODES:
        return False
    if cookie_file is None or not cookie_jar_is_usable(cookie_file):
        return True
    if missing_youtube_login_cookies(cookie_file):
        return is_youtube_url(url)
    return False


#: The four shapes a failed download comes in. Only the first is fixable here.
BlockCause = Literal["login", "ip", "site", "session"]


def classify_block(error: ExtractionError, url: str, cookie_file: Path | None) -> BlockCause:
    """Which kind of failure this is, for the record and for the digest.

    One rule, three consumers: the user's message, the operators' weekly summary,
    and the log. ``site`` is the honest bucket for everything else — a private
    video or a link nothing supports is not ours to fix, and counting those as
    blocks would bury the ones that are.
    """
    if error.code == "SESSION_STALE":
        return "session"
    if error.code in BLOCK_EXTRACTION_CODES:
        return "login" if login_looking_block(error, url, cookie_file) else "ip"
    return "site"


@dataclass(frozen=True)
class MediaInfo:
    source_url: str
    title: str
    platform: str
    webpage_url: str
    extension: str
    thumbnail: Optional[str]
    duration: Optional[int]
    filesize_approx: Optional[int]
    is_live: bool
    #: The height of the video that was actually produced, when the site reports one.
    #: It is what makes the caption honest about a quality *tier*: "up to 1080p" on a
    #: 720p upload is not a lie, but saying 720p is a fact.
    height: Optional[int] = None


@dataclass(frozen=True)
class DownloadResult:
    """What a download produced: one file — or a set of them, for a photo album.

    ``extra_paths`` is that set, empty for everything that produces a single file.
    Every path lives in the same job directory as ``file_path``, which is what makes
    the existing cleanup (one ``rmtree`` of the parent) still correct.
    """

    file_path: Path
    info: MediaInfo
    media_format: MediaFormat
    extra_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class SearchHit:
    """One *flat* search result: enough to choose between candidates, nothing more.

    Flat on purpose — a search that extracted every candidate would cost the user a
    dozen extractions for the one track they asked about. Title and length are all
    the choosing needs (see :mod:`services.spotify`), and both come from the search
    response itself.
    """

    url: str
    title: str
    duration_s: Optional[int]


def _to_search_hits(info: dict[str, Any] | None) -> list[SearchHit]:
    """Flatten yt-dlp's search answer into candidates we can choose between.

    Defensive on purpose: a search response is a playlist of *flat* entries, whose
    ``url`` may be absent on an older or newer yt-dlp, in which case the video id is
    what we have (and is enough to build the watch URL).
    """
    hits: list[SearchHit] = []
    for entry in (info or {}).get("entries") or []:
        if not isinstance(entry, dict):
            continue
        video_id = str(entry.get("id") or "")
        url = str(entry.get("url") or "")
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        if not url:
            continue
        duration = entry.get("duration")
        hits.append(
            SearchHit(
                url=url,
                title=str(entry.get("title") or video_id or url),
                duration_s=int(duration) if isinstance(duration, (int, float)) and duration else None,
            )
        )
    return hits


def _to_media_info(source_url: str, info: dict[str, Any]) -> MediaInfo:
    filesize = info.get("filesize") or info.get("filesize_approx") or 0
    if not filesize:
        filesize = sum(
            (f.get("filesize") or f.get("filesize_approx") or 0)
            for f in info.get("requested_formats") or []
        )
    return MediaInfo(
        source_url=source_url,
        title=str(info.get("title") or source_url),
        platform=str(info.get("extractor_key") or "unknown").lower().replace(":", ""),
        webpage_url=str(info.get("webpage_url") or source_url),
        extension=str(info.get("ext") or "mp4"),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        filesize_approx=int(filesize) if filesize else None,
        is_live=bool(info.get("is_live")),
        height=_reported_height(info),
    )


def _reported_height(info: dict[str, Any]) -> Optional[int]:
    """The real resolution of a download, however yt-dlp reported it.

    A merged download (video+audio) reports the height on the *selected video
    format*, which lives in ``requested_formats``; older or single-file cases put it
    on the top-level info. Either way it is a fact, or it is absent.
    """
    for candidate in (info, *(info.get("requested_formats") or [])):
        if not isinstance(candidate, dict):
            continue
        height = candidate.get("height")
        if isinstance(height, (int, float)) and height > 0:
            return int(height)
    return None


class ExtractorService:
    """Async wrapper around yt-dlp: metadata extraction + full downloads."""

    def __init__(
        self,
        download_dir: Path,
        *,
        timeout_s: int = 90,
        download_timeout_s: int = 1800,
        cookie_file: Path | None = None,
        proxy: str = "",
        pot_provider_url: str = "",
        cookies_from_browser: str = "",
        js_runtime: str = "auto",
        #: YouTube clients to ask for, in order. Empty = yt-dlp's own choice, which is
        #: the honest default for a caller that has not thought about it (a probe, a
        #: test); the app passes ``settings.ytdlp_youtube_clients``.
        youtube_clients: Sequence[str] = (),
        #: Force IPv4 for every request (yt-dlp's ``--force-ipv4``). Off by default
        #: here for the same reason: the app passes ``settings.ytdlp_force_ipv4``.
        force_ipv4: bool = False,
        #: Log YouTube in with OAuth2 (yt-dlp's ``username: 'oauth2'`` — the Smart-TV
        #: device flow; the token is prompted once and cached). Only meaningful when
        #: the installed yt-dlp (or a plugin) still implements the flow: current core
        #: refuses it by policy, and this flag then buys a clear error, not a login.
        use_oauth2: bool = False,
        #: Persistent yt-dlp cache directory (client ids, signatures, OAuth tokens).
        #: Empty = yt-dlp's own default. The container mounts a named volume at the
        #: configured path so a device-flow token survives recreation.
        cache_dir: str = "",
        #: Retries are opt-in here (the app passes ``settings.extractor_retry_*``)
        #: so a caller that just wants one attempt — a diagnostic probe, a test —
        #: does not inherit a sleeping retry loop.
        retry_attempts: int = 0,
        retry_backoff_s: float = 3.0,
        ffmpeg_location: Path | None = None,
    ) -> None:
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.download_timeout_s = download_timeout_s
        self.cookie_file = cookie_file
        #: Optional yt-dlp proxy (``http://user:pass@host:port`` / ``socks5://…``).
        #: YouTube answers "Sign in to confirm you're not a bot" even with a valid
        #: cookie jar when the host's IP is blocked, which a proxy can fix.
        self.proxy = proxy.strip()
        self.pot_provider_url = pot_provider_url.strip().rstrip("/")
        self.js_runtimes = detect_js_runtimes(js_runtime)
        #: Lower-cased like yt-dlp will read them, and de-duplicated in order: a
        #: repeated client costs a second player request for nothing.
        self.youtube_clients = tuple(
            dict.fromkeys(
                client.strip().lower() for client in youtube_clients if client and client.strip()
            )
        )
        unknown, _required, _free = youtube_client_facts(self.youtube_clients)
        if unknown:
            logger.warning(
                "yt-dlp does not know these YouTube clients (they will be skipped): %s — "
                "the names change between yt-dlp versions; `/doctor` lists what is in effect.",
                ", ".join(unknown),
            )
        #: IPv4-only when the app says so; see ``IPV4_ANY``.
        self.force_ipv4 = force_ipv4
        self.use_oauth2 = use_oauth2
        self.cache_dir = cache_dir.strip()
        #: Retries after a retryable failure, and the base of the exponential
        #: backoff between them (0 disables both).
        self.retry_attempts = max(0, retry_attempts)
        self.retry_backoff_s = max(0.0, retry_backoff_s)
        #: ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]``, probed lazily.
        self._browser_cookie_spec = cookies_from_browser.strip()
        self._browser_cookies_ok: bool | None = None
        self._warned_cookie_paths: set[Path] = set()
        #: yt-dlp rewrites its cookiefile when a download ends, so it is handed a
        #: writable copy of the configured jar instead of the jar itself.
        self._cookie_copy: Path | None = None
        self._cookie_copy_stamp: tuple[str, int, int] | None = None
        self._ffmpeg = shutil.which("ffmpeg") or (str(ffmpeg_location) if ffmpeg_location else None)
        if not self._ffmpeg:
            logger.warning(
                "ffmpeg not found on PATH — audio (MP3) downloads will be rejected; "
                "install ffmpeg for full functionality."
            )

    @property
    def ffmpeg_available(self) -> bool:
        return self._ffmpeg is not None

    @property
    def using_proxy(self) -> bool:
        return bool(self.proxy)

    @property
    def using_pot_provider(self) -> bool:
        return bool(self.pot_provider_url)

    @property
    def js_runtime_name(self) -> str:
        """Name of the JavaScript runtime in use, or ``"none"``."""
        return next(iter(self.js_runtimes), "none")

    @property
    def using_browser_cookies(self) -> bool:
        """True only when the configured browser profile really yields cookies.

        Probed once, then cached: the probe touches the filesystem and a broken
        spec must not be retried (and logged) on every task.
        """
        if not self._browser_cookie_spec:
            return False
        if self._browser_cookies_ok is None:
            self._browser_cookies_ok = browser_cookie_jar_is_usable(self._browser_cookie_spec)
        return self._browser_cookies_ok

    @property
    def extractor_args(self) -> dict[str, dict[str, list[str]]]:
        """yt-dlp ``extractor_args``: the optional PO-token provider, and the clients.

        The bgutil plugin reads ``youtubepot-bgutilhttp:base_url`` — the same key
        as ``--extractor-args "youtubepot-bgutilhttp:base_url=…"``. Without the
        plugin installed the key is simply ignored.

        Two settings share this one yt-dlp option, so they are *merged* here rather
        than each writing it: an assignment that dropped the other would be the
        worst kind of misconfiguration — one that reads as configured.
        """
        args: dict[str, dict[str, list[str]]] = {}
        if self.pot_provider_url:
            args["youtubepot-bgutilhttp"] = {"base_url": [self.pot_provider_url]}
        if self.youtube_clients:
            # ``player_client`` is the key yt-dlp reads (``_configuration_arg``);
            # anything else — ``client``, for instance — is never looked at, so it
            # would look like a spoof and change nothing.
            args["youtube"] = {"player_client": list(self.youtube_clients)}
        return args

    @staticmethod
    def is_url_supported(url: str) -> bool:
        """Cheap offline probe: does any non-generic yt-dlp extractor match?"""
        try:
            return any(
                extractor.suitable(url) and extractor.IE_NAME != "generic"
                for extractor in yt_dlp.gen_extractors()
            )
        except Exception:  # never fail the probe — fall through to real extraction
            return True

    def _base_opts(
        self,
        *,
        extract_only: bool,
        media_format: MediaFormat = "video",
        quality: object = "",
        allow_cookies: bool = True,
    ) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,  # progress is delivered via progress_hooks only
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 3,
            "fragment_retries": 3,
            "format": format_selector(media_format, quality),
            "merge_output_format": MERGE_OUTPUT_FORMAT,
            # Resolution first, then HEVC on ties — consistent with the selector.
            "format_sort": ["res", "vcodec:hevc"],
        }
        if self.proxy:
            opts["proxy"] = self.proxy
        if self.js_runtimes:
            opts["js_runtimes"] = self.js_runtimes
        if args := self.extractor_args:
            opts["extractor_args"] = args
        if self.force_ipv4:
            # yt-dlp's ``--force-ipv4``: its socket layer filters resolved addresses
            # by this family, so IPv6 is never attempted (see ``IPV4_ANY``).
            opts["source_address"] = IPV4_ANY
        if self.cache_dir:
            # ``--cache-dir``: where client ids, signatures — and, under an OAuth
            # plugin, the device-flow token — are kept. Pointless without the flag
            # on a stock install; cheap either way.
            opts["cache_dir"] = self.cache_dir
        if self.use_oauth2:
            # The Smart-TV device flow: yt-dlp prompts ``go to
            # https://www.google.com/device and enter code XXX-YYY-ZZZ`` on its
            # progress reporter, then polls until the code is entered. The
            # password is empty *by contract* — the flow authenticates the
            # device, not an account. The plugin's own guidance is to avoid
            # running it *with* a cookie jar (the jar's anonymous identifiers can
            # trip the token exchange), so a deployment with both configured gets
            # one loud warning rather than a silent conflict.
            if allow_cookies and (self.cookie_file is not None or self.using_browser_cookies):
                logger.warning(
                    "OAuth2 login is enabled alongside a cookie jar — yt-dlp's OAuth "
                    "flow is documented to misbehave when account/anonymous cookies "
                    "are sent in the same request. If logins fail, unset COOKIE_FILE "
                    "or turn YTDLP_USE_OAUTH2 off."
                )
            opts["username"] = OAUTH2_USERNAME
            opts["password"] = OAUTH2_PASSWORD
        # Browser cookies are merged with the jar file by yt-dlp; whichever is
        # missing is skipped, so both can be configured safely. ``allow_cookies=False``
        # is the second opinion a stale session sometimes needs: the same request
        # without the jar (see ``_search_attempt``).
        if allow_cookies and self.using_browser_cookies:
            opts["cookiesfrombrowser"] = parse_browser_spec(self._browser_cookie_spec)
        if allow_cookies and self.cookie_file is not None:
            writable_jar = self._writable_cookie_file()
            if writable_jar is not None:
                opts["cookiefile"] = str(writable_jar)
            else:
                self.warn_if_cookies_unusable()
        if extract_only:
            opts["skip_download"] = True
        return opts

    def _translate(self, exc: DownloadError) -> ExtractionError:
        """Map a yt-dlp failure, adding an operator hint for bot-detection blocks."""
        error = _map_download_error(exc)
        if error.code == "SESSION_STALE":
            logger.warning(
                "YouTube rejected the request as stale (\"the page needs to be reloaded\") — "
                "usually a rotated session, an unusable visitor binding, or a missing PO token. "
                "A retry often clears it; a fresh cookie jar and a PO-token provider "
                "(YTDLP_POT_PROVIDER_URL) make it stop. `python scripts/youtube_doctor.py` says which."
            )
        if error.code == DRM_PROTECTED_CODE:
            logger.info(
                "yt-dlp refused a DRM-listed site by policy — the fallback engine is the "
                "only route that can still serve this link."
            )
        if error.code == "EXTRACTOR_BLOCKED":
            if hint := youtube_login_hint(self.cookie_file):
                # The common misdiagnosis: an unusable *login* looks exactly like
                # an IP ban, and people buy a proxy to fix the wrong thing.
                logger.warning("Extraction was blocked, and %s", hint)
            elif not self.using_cookies:
                logger.warning(
                    "Extraction was blocked (often YouTube's bot check). Fixes, in order: "
                    "a real cookies.txt (COOKIE_FILE, default ./cookies.txt), a PO-token "
                    "provider (YTDLP_POT_PROVIDER_URL), or a YTDLP_PROXY on a non-flagged IP."
                )
        return error

    @property
    def youtube_login_ready(self) -> bool:
        """True when the configured jar would actually sign YouTube in."""
        if not self.using_cookies:
            return False
        return not missing_youtube_login_cookies(self.cookie_file)

    @property
    def using_cookies(self) -> bool:
        """True when a cookie jar with real cookies is configured.

        Re-checked on every call so a jar dropped in while the bot is running is
        picked up without a restart.
        """
        return cookie_jar_is_usable(self.cookie_file)

    def _writable_cookie_file(self) -> Path | None:
        """Path handed to yt-dlp: a writable copy of the configured jar.

        ``YoutubeDL.close()`` saves the jar back (``save_cookies``), so a
        read-only file — exactly what a mounted ``cookies.txt:ro`` is — makes
        *every* download end in ``PermissionError`` at the very end, after the
        bytes were already fetched. Copying into the download area keeps
        downloads working, leaves the mounted file untouched (an exporter is the
        only writer), and still honours a fresh export: the copy is refreshed
        whenever the source's size or mtime changes.
        """
        source = self.cookie_file
        if source is None or not cookie_jar_is_usable(source):
            return None
        try:
            stat = source.stat()
        except OSError:
            return None
        stamp = (str(source), stat.st_mtime_ns, stat.st_size)
        if self._cookie_copy is not None and self._cookie_copy_stamp == stamp:
            return self._cookie_copy
        copy_dir = self.download_dir / ".cookies"
        target = copy_dir / "cookies.txt"
        try:
            copy_dir.mkdir(parents=True, exist_ok=True)
            # Written to a private name and moved into place: yt-dlp runs in
            # worker threads, and a reader must never catch a half-written jar
            # (it parses as "no cookies" and turns into a bogus block).
            temp = copy_dir / f".cookies-{os.getpid()}-{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(source, temp)
                os.replace(temp, target)
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            # A read-only *source* is the whole point of the copy, but a
            # read-only downloads/ would leave no writable space at all. Hand
            # yt-dlp the jar directly and let it surface normally if it cannot
            # write it back.
            logger.warning(
                "could not copy COOKIE_FILE=%s into %s (%s) — passing it to yt-dlp "
                "directly; yt-dlp rewrites the jar when a download ends and will fail "
                "on a read-only file.",
                source,
                copy_dir,
                exc,
            )
            self._cookie_copy = source
            self._cookie_copy_stamp = stamp
            return source
        logger.debug("using a writable copy of COOKIE_FILE=%s at %s", source, target)
        self._cookie_copy = target
        self._cookie_copy_stamp = stamp
        return target

    def cookie_jar_state(self) -> CookieJarState:
        """Describe the configured jar, its mount, and the freshness of the copy.

        Deliberately read-only: a diagnosis must not create the copy it is
        talking about — the next download does that, and says so in the log. The
        ``in_sync`` verdict is exact when this instance has already handed the jar
        to yt-dlp (the running bot), and falls back to comparing timestamps in a
        one-shot process (the CLI doctor) where nothing has been copied yet.
        """
        source = self.cookie_file
        copy_path = self.download_dir / ".cookies" / "cookies.txt"
        copy_refreshed_at: float | None = None
        try:
            if copy_path.is_file():
                copy_refreshed_at = copy_path.stat().st_mtime
        except OSError:
            copy_refreshed_at = None

        blank = CookieJarState(
            path=source,
            kind="missing",
            size_bytes=None,
            exported_at=None,
            mount=None,
            writable=False,
            cookie_count=0,
            missing_login=(),
            copy_path=copy_path if copy_refreshed_at is not None else None,
            copy_refreshed_at=copy_refreshed_at,
            in_sync=None,
        )
        if source is None:
            return blank
        if source.is_dir():
            return replace(blank, kind="directory", mount=mount_info_for(source))
        if not source.exists():
            return replace(blank, mount=mount_info_for(source))
        try:
            stat = source.stat()
        except OSError:
            return blank

        usable = cookie_jar_is_usable(source)
        mount = mount_info_for(source)
        writable = not (mount is not None and mount.read_only) and os.access(source, os.W_OK)
        in_sync: bool | None = None
        if copy_refreshed_at is not None:
            if self._cookie_copy_stamp is not None:
                in_sync = self._cookie_copy_stamp == (str(source), stat.st_mtime_ns, stat.st_size)
            else:
                in_sync = copy_refreshed_at >= stat.st_mtime

        return CookieJarState(
            path=source,
            kind="ok" if usable else "unusable",
            size_bytes=stat.st_size,
            exported_at=stat.st_mtime,
            mount=mount,
            writable=writable,
            cookie_count=count_netscape_cookies(source),
            missing_login=missing_youtube_login_cookies(source),
            copy_path=copy_path if copy_refreshed_at is not None else None,
            copy_refreshed_at=copy_refreshed_at,
            in_sync=in_sync,
        )

    def warn_if_cookies_unusable(self) -> None:
        """Explain an unusable cookie jar once per path, not once per download.

        Public so startup (and ``boot_check``) can surface it before the first
        download does — a directory left by a bind mount is silent otherwise.
        """
        if self.cookie_file is None or self.cookie_file in self._warned_cookie_paths:
            return
        self._warned_cookie_paths.add(self.cookie_file)
        if not self.cookie_file.exists():
            # The deployed default: no jar on the host. Cookies only matter for
            # sites that demand a login, so this is a note, not a failure.
            logger.warning(
                "COOKIE_FILE=%s does not exist — ignoring it. Everything that does not "
                "need a login keeps working; for YouTube and age-restricted links, export "
                "the jar (python scripts/export_cookies.py) — it is picked up on the next "
                "download, so no restart is needed.",
                self.cookie_file,
            )
            return
        if self.cookie_file.is_dir():
            # The Docker trap: bind-mounting a *file* whose host path is missing
            # makes Docker create a directory at that path instead.
            logger.warning(
                "COOKIE_FILE=%s is a directory, not a cookie jar — Docker creates one "
                "where a bind-mounted file was expected. Export the cookies "
                "(python scripts/export_cookies.py); they are picked up on the next download.",
                self.cookie_file,
            )
            return
        if not self.cookie_file.is_file():
            reason = "is not a regular file"
        elif self.cookie_file.stat().st_size == 0:
            reason = "is empty"
        else:
            reason = "does not contain any Netscape cookies"
        logger.warning(
            "COOKIE_FILE=%s %s — ignoring it (an empty or malformed jar makes yt-dlp "
            "fail on every link). Re-export the cookies and restart, or leave the "
            "setting empty.",
            self.cookie_file,
            reason,
        )

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    async def extract(self, url: str) -> MediaInfo:
        """Fetch media metadata without downloading. Never blocks the loop."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._extract_sync, url),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            raise ExtractionError("TIMEOUT", "بررسی لینک بیش از حد طول کشید؛ دوباره تلاش کنید.") from None

    def _with_retries(self, what: str, attempt_fn: Callable[[], T]) -> T:
        """Run ``attempt_fn``, retrying only the failures that deserve it.

        Called from inside a worker thread (never on the event loop), so the
        backoff can simply sleep. Every retry is logged with its attempt number
        and delay: a silent retry would make a slow download unexplainable.
        """
        for attempt in range(self.retry_attempts):
            try:
                return attempt_fn()
            except ExtractionError as exc:
                if exc.code not in RETRYABLE_EXTRACTION_CODES:
                    raise
                delay = self.retry_backoff_s * (2**attempt)
                logger.info(
                    "%s failed with %s — retrying in %.1fs (attempt %d of %d)",
                    what,
                    exc.code,
                    delay,
                    attempt + 2,
                    self.retry_attempts + 1,
                )
                if delay:
                    time.sleep(delay)
        # Final attempt: its failure is the one the user gets to see.
        return attempt_fn()

    def _extract_sync(self, url: str) -> MediaInfo:
        return self._with_retries("extracting metadata", lambda: self._extract_attempt(url))

    def _extract_attempt(self, url: str) -> MediaInfo:
        opts = self._base_opts(extract_only=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            raise self._translate(exc) from exc
        if not info:
            raise ExtractionError("GENERAL", "سایت مبدأ پاسخ معتبری برنگرداند.")
        if info.get("_type") == "playlist":
            raise ExtractionError("PLAYLIST_NOT_SUPPORTED", "دانلود لیست پخش (playlist) پشتیبانی نمی‌شود.")
        if info.get("is_live"):
            raise ExtractionError("LIVE_STREAM", "پخش زنده قابل دانلود نیست.")
        return _to_media_info(url, info)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        """Search YouTube on the *same* configured path downloads use.

        That is the whole point of routing an unservable link through a search: the
        cookies, the PO token provider and the proxy this bot is tuned with are
        exactly what decides whether a song can be found as well as downloaded — a
        search that ran somewhere else would answer differently from the download
        and turn one mystery into two.

        Retried (and re-tried without the jar) like any other extraction, because a
        *stale session* refuses a search exactly the way it refuses a download — and
        this one runs before the user's download has even started, so failing here is
        the difference between a Spotify link that works and one that never gets a
        chance. The retry budget is the same ``EXTRACTOR_RETRY_*`` pair the rest of
        the engine uses, so the extra wait is bounded and predictable.
        """
        return await asyncio.wait_for(
            asyncio.to_thread(self._search_sync, query, limit),
            timeout=self.timeout_s,
        )

    def _search_sync(self, query: str, limit: int) -> list[SearchHit]:
        return self._with_retries(
            "searching YouTube", lambda: self._search_attempt(query, limit)
        )

    def _search_attempt(self, query: str, limit: int) -> list[SearchHit]:
        """One search — and, for a session our jar is the problem in, one without it.

        ``SESSION_STALE`` ("the page needs to be reloaded") and a bot-check refusal
        are both things yt-dlp's own troubleshooting answers with "retry without the
        cookies": a rotated or unusable session *is* the trigger, and an anonymous
        search often succeeds where the jar gets in the way. So when a jar was sent
        and the failure is one of those two, the search is repeated once with no
        cookies at all — bounded, logged, and only ever for a search (a download that
        fails that way keeps its existing behaviour: there the block is the *answer*,
        and it drives the user's message, the admin alert and the jar refresh).
        """
        try:
            return self._search_with_opts(query, limit, allow_cookies=True)
        except ExtractionError as exc:
            if exc.code not in _ANONYMOUS_RETRY_CODES or not self.using_cookies:
                raise
            logger.warning(
                "search failed with %s while a cookie jar was in use — repeating it "
                "without the jar (an unusable session is a known cause of both).",
                exc.code,
            )
            return self._search_with_opts(query, limit, allow_cookies=False)

    def _search_with_opts(self, query: str, limit: int, *, allow_cookies: bool) -> list[SearchHit]:
        opts = self._base_opts(extract_only=True, allow_cookies=allow_cookies)
        opts.update({"extract_flat": "in_playlist", "playlistend": limit})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except DownloadError as exc:
            raise self._translate(exc) from exc
        return _to_search_hits(info)

    # ------------------------------------------------------------------
    # Full download
    # ------------------------------------------------------------------

    async def download(
        self,
        url: str,
        media_format: MediaFormat,
        quality: object = "",
        progress_hook: ProgressHook | None = None,
    ) -> DownloadResult:
        """Download media to a per-job directory and return the produced file.

        ``quality`` picks the tier: a height ceiling for video, and for audio the
        difference between the untouched m4a stream and an MP3 that ffmpeg has to
        produce (the only case that needs ffmpeg at all).
        """
        tier = normalize_quality(quality, media_format)
        if media_format == "audio" and tier == "mp3" and not self.ffmpeg_available:
            raise ExtractionError(
                "FFMPEG_REQUIRED",
                "تبدیل به MP3 نیاز به نصب ffmpeg دارد؛ لطفاً بعداً دوباره تلاش کنید.",
            )
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._download_sync, url, media_format, tier, progress_hook
                ),
                timeout=self.download_timeout_s,
            )
        except asyncio.TimeoutError:
            raise ExtractionError("TIMEOUT", "دانلود بیش از حد طول کشید؛ دوباره تلاش کنید.") from None

    def _download_sync(
        self,
        url: str,
        media_format: MediaFormat,
        quality: str,
        progress_hook: ProgressHook | None,
    ) -> DownloadResult:
        return self._with_retries(
            "downloading",
            lambda: self._download_attempt(url, media_format, quality, progress_hook),
        )

    def _download_attempt(
        self,
        url: str,
        media_format: MediaFormat,
        quality: str,
        progress_hook: ProgressHook | None,
    ) -> DownloadResult:
        # Each job gets its own directory so concurrent workers never collide.
        target_dir = self.download_dir / f"job-{uuid.uuid4().hex[:10]}"
        target_dir.mkdir(parents=True, exist_ok=True)

        opts = self._base_opts(
            extract_only=False, media_format=media_format, quality=quality
        )
        opts["outtmpl"] = str(target_dir / "%(title).120B [%(id)s].%(ext)s")
        if media_format == "audio" and quality == "mp3":
            # Only the MP3 tier re-encodes; m4a is whatever the site already serves.
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
        if progress_hook:
            opts["progress_hooks"] = [progress_hook]

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except DownloadError as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise self._translate(exc) from exc

        if not info or info.get("_type") == "playlist":
            shutil.rmtree(target_dir, ignore_errors=True)
            raise ExtractionError("PLAYLIST_NOT_SUPPORTED", "دانلود لیست پخش پشتیبانی نمی‌شود.")

        files = sorted(target_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        if not files:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise ExtractionError("GENERAL", "فایل دانلودشده پیدا نشد.")
        return DownloadResult(file_path=files[-1], info=_to_media_info(url, info), media_format=media_format)
