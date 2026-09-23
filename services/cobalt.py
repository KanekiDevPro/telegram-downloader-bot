"""Fallback extractor: get a direct download link from a Cobalt instance.

yt-dlp stays the primary engine — it knows titles, formats and a quality ladder a
generic download API cannot. But it has one failure shape this deployment keeps
hitting: the *site* refuses us (a flagged IP, or a jar it will not accept), and that
refusal costs the user the whole download. A Cobalt instance runs on somebody
else's connection and often simply works, so a blocked link is handed over instead
of answered with an error.

Deliberately narrow: JSON in, a direct URL out, stream it to disk. Nothing here
knows about cookies, PO tokens or yt-dlp, and nothing here raises anything but
:class:`CobaltError` — the worker decides what the user hears, and when the
fallback fails too, the original yt-dlp diagnosis is the honest one to show.

The contract is small (https://github.com/imputnet/cobalt): the response carries
``status`` of ``stream`` | ``redirect`` | ``tunnel`` — all with a ``url`` — or
``picker`` (an album; not supported) or an error body. Two schemas are in the wild:
the older request shape (``vQuality`` / ``filenamePattern``) and the newer one
(``videoQuality`` / ``filenameStyle`` / ``downloadMode``). We send the documented
older shape first and replay the newer one when an instance rejects the fields,
because which schema an arbitrary instance accepts is not knowable in advance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from core.utils import MediaFormat, normalize_quality, sanitize_filename

logger = logging.getLogger(__name__)

#: Where the *older* resolve request goes. Cobalt v10 moved the API to the root of
#: the instance and renamed the request fields; v7 (``/api/json`` + ``vQuality``)
#: was shut down on the public instance in November 2024. Which one an arbitrary
#: instance speaks is not knowable in advance — and a self-hosted one may be either
#: — so both are tried, most-current first is *not* assumed: the documented v7 shape
#: is sent first because the older instances out there only speak it, and a live
#: instance answers the wrong shape with a message that says exactly that.
LEGACY_RESOLVE_PATH = "/api/json"
MODERN_RESOLVE_PATH = ""

#: Cobalt's own word for "no ceiling" — what this bot's ``best`` tier means. The two
#: engines have to agree about the request: yt-dlp is asked for ``bestvideo`` with no
#: height filter, so the fallback must not quietly answer with a rung of its own
#: choosing (which is what a fixed ``1080`` did before tiers existed).
MAX_QUALITY = "max"

#: What a *probe* asks for: enough to prove an instance resolves a link, without the
#: metadata of a 4K one. Never used for a download a user is waiting on.
PROBE_QUALITY = "1080"

#: How much bigger than the ceiling we may download before giving up. The point is
#: to stop early rather than to be exact: the worker re-checks the finished file.
_READ_CHUNK = 256 * 1024

#: Extensions we trust from a content type when neither Cobalt nor the URL names a
#: file. Telegram is happiest with these, and mp4/mp3 are what Cobalt produces.
_CONTENT_TYPE_EXT: dict[str, str] = {
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/webm": "weba",
    "image/jpeg": "jpg",
    "image/png": "png",
}

#: Cobalt's error codes, in the words an operator reading a log needs. They stay in
#: the log — the user gets the yt-dlp diagnosis, not this.
_ERROR_HINTS: dict[str, str] = {
    "error.api.invalid_body": "درخواست با این نمونه سازگار نبود (شکل API عوض شده؟)",
    "error.api.link.invalid": "این نمونه لینک را نمی‌شناسد",
    "error.api.link.unsupported": "این سایت برای این نمونه پشتیبانی نمی‌شود",
    "error.api.rate_exceeded": "نمونهٔ کوبالت محدودیت نرخ دارد",
    "error.api.auth.key.missing": "این نمونه کلید می‌خواهد (COBALT_API_KEY)",
    "error.api.auth.key.invalid": "کلید COBALT_API_KEY پذیرفته نشد",
    "error.api.auth.jwt.missing": (
        "این نمونه احراز هویت می‌خواهد (کلید COBALT_API_KEY یا نمونهٔ خودتان)"
    ),
    "error.api.auth.jwt.invalid": "توکن احراز هویت این نمونه پذیرفته نشد",
    "error.api.turnstile.missing": "این نمونه عبور از Turnstile می‌خواهد — نمونهٔ خودتان را اجرا کنید",
    "error.api.content.video.unavailable": "کوبالت هم نتوانست این محتوا را بگیرد",
    "error.api.content.video.live": "پخش زنده از سمت کوبالت هم قابل دانلود نیست",
    "error.api.fetch.critical": "کوبالت نتوانست به منبع وصل شود",
    "error.api.fetch.empty": "کوبالت پاسخ خالی گرفت",
    "error.api.fetch.fail": "کوبالت نتوانست این سرویس را بخواند (این نمونه پشتیبانی/کوکی ندارد)",
    "error.api.fetch.rate": "منبع، کوبالت را هم محدود کرد",
    # YouTube is special: a self-hosted instance needs a *session* (cookies.json or
    # a po-token server) for it, exactly like yt-dlp needs the jar. It is a gap in
    # one service, not a broken instance — so it is named, not quarantined.
    "error.api.youtube.login": (
        "این نمونه برای یوتیوب سشن/کوکی ندارد (cookies.json کوبالت یا YOUTUBE_SESSION_SERVER)"
    ),
    "error.api.youtube.session": "سشن یوتیوب این نمونه پذیرفته نشد — cookies.json را تازه کنید",
}

#: Upstream codes that mean "*this instance* cannot serve YouTube at all", as
#: opposed to "this link is unavailable". Deliberately **not** in
#: ``_INSTANCE_ERROR_CODES``: a YouTube-only gap must never silence the fallback for
#: tiktok/instagram/twitter, where the same instance may be perfectly healthy.
_YOUTUBE_SESSION_PREFIX = "error.api.youtube."

#: Upstream codes that mean "this instance cannot serve anyone right now", as opposed
#: to "this particular link cannot be served". Only the first kind is worth stopping
#: for: a private video must not silence a perfectly good fallback.
_INSTANCE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "error.api.auth.key.missing",
        "error.api.auth.key.invalid",
        "error.api.auth.jwt.missing",
        "error.api.auth.jwt.invalid",
        "error.api.turnstile.missing",
        "error.api.rate_exceeded",
        "error.api.fetch.rate",
        "error.api.fetch.critical",
    }
)

#: The subset that means "configure a key (or run your own instance)" — the single
#: most common reason a configured fallback answers nothing at all today.
_AUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "error.api.auth.key.missing",
        "error.api.auth.key.invalid",
        "error.api.auth.jwt.missing",
        "error.api.auth.jwt.invalid",
        "error.api.turnstile.missing",
    }
)

#: Fields the newer API renamed, so a schema rejection can be recognised instead of
#: being reported as "the instance is broken".
_SCHEMA_HINTS = ("unknown field", "invalid field", "vquality", "filenamestyle", "expected")
#: What the public instance answers the retired v7 endpoint with — a working v10+
#: instance is on the other side of this message, not broken.
_RETIRED_HINTS = ("shut down", "v7 api", "no longer")

#: How long an instance is left alone after it fails as an *instance* (auth, rate
#: limit, unreachable). Without it a deployment pointed at an instance that needs a
#: key would spend one doomed round trip and one log line per blocked link; with it,
#: the second blocked link is answered immediately and the operator has one clear
#: message instead of a wall of them.
QUARANTINE_S = 600.0


class CobaltError(Exception):
    """A fallback attempt that did not produce a file.

    ``code`` is stable so a caller (and a test) can be specific without matching
    Persian text: ``DISABLED``, ``UNREACHABLE``, ``REFUSED``, ``ERROR``,
    ``NO_MEDIA``, ``TIMEOUT``, ``BAD_RESPONSE``, ``TOO_LARGE``.

    ``instance`` says where the fault is: the instance itself (down, unaware of the
    API, refusing us) or this one link (private, an album, too large). It decides
    whether the fallback is left alone for a while afterwards.

    ``needs_auth`` is the one diagnosis worth its own flag rather than a text
    match: a *doctor* report has to say "this instance wants a key" without
    guessing from Persian wording, and it is the difference between "run your own"
    and "the host cannot reach anything".

    ``upstream`` is the instance's *own* code (``error.api.youtube.login``), when
    there was one. Our codes are about the fallback, not about the site: a doctor
    report that must distinguish "no YouTube session" from "this link is gone"
    needs the original, and Persian wording is not something to match on.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        instance: bool = False,
        needs_auth: bool = False,
        upstream: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.instance = instance
        self.needs_auth = needs_auth
        self.upstream = upstream

    @property
    def youtube_session_missing(self) -> bool:
        """Whether the instance is only missing its YouTube session."""
        return self.upstream.startswith(_YOUTUBE_SESSION_PREFIX)


@dataclass(frozen=True)
class CobaltPart:
    """One item of a ``picker``: a photo (or video) of a multi-media post.

    ``kind`` is Cobalt's own word for it (``photo`` / ``video`` / ``gif``) and stays
    for the log; the *file* is what decides how Telegram receives it, because only
    the downloaded bytes and their name are certain (see ``services.worker``).
    """

    url: str
    filename: Optional[str] = None
    kind: str = ""
    #: Only a single stream reports its size up front; a picker item does not.
    size_bytes: Optional[int] = None


@dataclass(frozen=True)
class CobaltMedia:
    """A resolved download: where the bytes are, and whatever name came with them.

    A *set* is also possible: Cobalt answers a multi-media post with ``picker``
    (twitter's photo albums, instagram's carousels) and then :attr:`parts` holds
    every item while :attr:`url` stays empty. One request can therefore produce
    several files, which is why the download methods come in a plural form.
    """

    url: str = ""
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    parts: tuple[CobaltPart, ...] = ()

    @property
    def items(self) -> tuple[CobaltPart, ...]:
        """Every item to fetch: the parts of a picker, or the single stream."""
        if self.parts:
            return self.parts
        return (
            CobaltPart(url=self.url, filename=self.filename, size_bytes=self.size_bytes),
        )


def video_quality_param(quality: object) -> str:
    """The tier this bot offers, in Cobalt's vocabulary (``max`` / ``1080`` / …).

    Same scale on purpose: both engines count in pixels, and "max" is what both call
    "no ceiling". A tier Cobalt does not know would be a silent fallback to its own
    default, which is why only known values are mapped — anything else becomes the
    configured default rather than a guess.
    """
    tier = normalize_quality(quality, "video")
    return MAX_QUALITY if tier == "best" else tier


def audio_format_param(media_format: MediaFormat, quality: object) -> str:
    """Which audio format to ask Cobalt for.

    Cobalt names formats, not quality levels — the level the user picked is the
    primary engine's business (and its alone), so every tier of a container maps
    to that container's name here. Two translations carry the history: ``m4a``
    means "the original stream, untouched" and Cobalt spells that ``best`` (it
    never re-encodes then, which is exactly the promise that button makes — the
    file may come back as ``.opus`` and Telegram still plays it as audio), and
    every ``mp3`` level is Cobalt's own ``mp3`` re-encode.
    """
    if media_format != "audio":
        return ""
    tier = normalize_quality(quality, "audio")
    if tier == "wav":
        return "wav"
    if tier.startswith("opus"):
        return "opus"
    if tier.startswith("m4a"):
        return "best"
    return "mp3"


def _legacy_payload(url: str, media_format: MediaFormat, quality: object = "") -> dict[str, Any]:
    """The request shape Cobalt documents (and older instances require)."""
    payload: dict[str, Any] = {
        "url": url,
        "vQuality": video_quality_param(quality),
        "filenamePattern": "nerd",
    }
    if media_format == "audio":
        # Server-side audio extraction: it also means the fallback needs no ffmpeg,
        # which matters when the primary path just lost its only extractor.
        payload["isAudioOnly"] = True
        payload["aFormat"] = audio_format_param(media_format, quality)
    return payload


def _modern_payload(url: str, media_format: MediaFormat, quality: object = "") -> dict[str, Any]:
    """The same request in the newer schema, for instances that renamed the fields.

    ``pretty`` rather than ``nerd``: the current API validates this field against a
    closed list and answers a request containing ``nerd`` with
    ``error.api.invalid_body`` — verified against a live v10 instance, which is the
    kind that now ships embedded in ``docker-compose.yml``. The pretty style still
    names the file after the media, which is what the caption reads.
    """
    payload: dict[str, Any] = {
        "url": url,
        "videoQuality": video_quality_param(quality),
        "filenameStyle": "pretty",
        "downloadMode": "auto",
    }
    if media_format == "audio":
        payload["audioFormat"] = audio_format_param(media_format, quality)
        payload["downloadMode"] = "audio"
    return payload


#: Every API shape we know, in the order they are tried on a fresh instance:
#: the documented v7 request first (older self-hosted instances speak only that),
#: then the current one, where v10 moved the API to the root of the instance.
_ENDPOINTS: tuple[tuple[str, Callable[..., dict[str, Any]]], ...] = (
    (LEGACY_RESOLVE_PATH, _legacy_payload),
    (MODERN_RESOLVE_PATH, _modern_payload),
)

#: How each shape is named in a report (same order as ``_ENDPOINTS``).
_DIALECTS: tuple[str, ...] = ("v7", "v10")


def _hint(body: Any) -> str:
    """The instance's own error code, translated for the log."""
    code = _error_code(body)
    if code in _ERROR_HINTS:
        return f"{_ERROR_HINTS[code]} ({code})"
    if code:
        return code
    return "پاسخ نامعتبر از نمونهٔ کوبالت"


def _error_code(body: Any) -> str:
    """The upstream error code of a response, if it has one."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or "")


def _body_text(body: Any) -> str:
    """Whatever the instance said about a response, as one line of text."""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    for key in ("text", "message"):
        if body.get(key):
            return str(body[key])
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


def _node_urls(base_url: str | Sequence[str]) -> tuple[str, ...]:
    """The configured instances, cleaned: trimmed, no trailing slash, no duplicates.

    Takes one address or a list of them so the single-instance callers (the CLI,
    the tests, a one-off probe) keep reading like before while the bot itself can
    pass a pool.
    """
    candidates: Iterable[object] = (base_url,) if isinstance(base_url, str) else base_url
    urls: list[str] = []
    for candidate in candidates:
        url = candidate.strip().rstrip("/") if isinstance(candidate, str) else ""
        if url and url not in urls:
            urls.append(url)
    return tuple(urls)


@dataclass
class _Node:
    """One instance in the pool, with the two things worth remembering about it.

    ``endpoint_index`` is the dialect that last answered it — a live instance is
    probed once per process, not once per blocked link — and ``quarantine_until`` is
    when it may be tried again. This state used to live on the service; a pool needs
    one of each *per node*, which is the whole reason it moved here.
    """

    url: str
    endpoint_index: int | None = None
    quarantine_until: float = 0.0
    quarantine_reason: str = ""

    @property
    def quarantined(self) -> bool:
        return time.monotonic() < self.quarantine_until


@dataclass(frozen=True)
class CobaltNodeState:
    """A node as a report reads it: where it is, what it speaks, whether it is out."""

    url: str
    dialect: str | None = None
    quarantined: bool = False
    reason: str = ""
    active: bool = False


class CobaltService:
    """An async client for one or more Cobalt instances.

    One client is shared by every worker, so the HTTP session (and its connection
    pool) is created lazily and closed once at shutdown.

    **Why a pool.** The embedded instance runs on this host's network address, so it
    inherits whatever YouTube thinks of this IP: on a flagged VPS both engines fail
    the same way, and a fallback that fails identically is not a fallback. A pool
    makes the second attempt a genuinely different one — another machine, another
    address, another chance — and the order is deliberate: the instance you control
    first (fast, private, free), your own mirrors next, the public instance last (it
    may demand a key, rate-limit us, and it sees the link). Nodes that fail as
    *instances* are quarantined individually, so the pool degrades to what still
    works instead of turning one bad node into no fallback at all.
    """

    def __init__(
        self,
        base_url: str | Sequence[str],
        *,
        api_key: str = "",
        timeout_s: float = 30.0,
        download_timeout_s: float = 1800.0,
        proxy: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        urls = _node_urls(base_url)
        self.base_urls: tuple[str, ...] = urls
        #: The instance every report names first, and the only one the probes that
        #: are *about the stack itself* (has cobalt reloaded the cookies? what is
        #: its start time?) are sent to.
        self.base_url = urls[0] if urls else ""
        # A placeholder node for the disabled case keeps every accessor below free
        # of ``if self._nodes`` — an empty ``COBALT_API_URL`` is a configuration, not
        # an object with no state.
        self._nodes: list[_Node] = [_Node(url=url) for url in urls] or [_Node(url="")]
        self._primary = self._nodes[0]
        self._active_url = ""
        self._last_quarantine = ""
        self.api_key = api_key.strip()
        self.timeout_s = timeout_s
        self.download_timeout_s = download_timeout_s
        self.proxy = proxy or None
        self._session = session
        self._owns_session = session is None

    # ``_endpoint_index`` / ``_quarantine_until`` / ``_quarantine_reason`` name the
    # *primary* node's memory. They used to be plain attributes and are still how the
    # older reports and tests read the instance an operator configured; the extra
    # nodes keep theirs in ``_nodes``.
    @property
    def _endpoint_index(self) -> int | None:
        return self._primary.endpoint_index

    @_endpoint_index.setter
    def _endpoint_index(self, value: int | None) -> None:
        self._primary.endpoint_index = value

    @property
    def _quarantine_until(self) -> float:
        return self._primary.quarantine_until

    @_quarantine_until.setter
    def _quarantine_until(self, value: float) -> None:
        self._primary.quarantine_until = value

    @property
    def _quarantine_reason(self) -> str:
        return self._primary.quarantine_reason

    @_quarantine_reason.setter
    def _quarantine_reason(self, value: str) -> None:
        self._primary.quarantine_reason = value

    @property
    def enabled(self) -> bool:
        """Whether any instance is configured (``COBALT_API_URL`` / fallbacks)."""
        return bool(self.base_urls)

    @property
    def quarantined(self) -> bool:
        """Whether *every* instance is being left alone (see ``QUARANTINE_S``).

        With one node this is the old meaning exactly. With a pool, the question a
        caller is really asking is "is there any net left?" — and a report that said
        "quarantined" while three healthy nodes could still answer would be a lie.
        """
        return all(node.quarantined for node in self._nodes)

    @property
    def available(self) -> bool:
        """Configured *and* with at least one node not being left alone."""
        return self.enabled and any(not node.quarantined for node in self._nodes)

    @property
    def quarantine_reason(self) -> str:
        """Why the pool is being left alone (the most recent cause, in full)."""
        return self._last_quarantine or self._primary.quarantine_reason

    @property
    def dialect(self) -> str | None:
        """Which API shape last answered: ``v7``, ``v10``, or ``None``.

        Reported by the doctor: two shapes exist in the wild, and which one an
        operator's instance speaks is the first thing to know when a request works
        by hand but not from here. With a pool it is the shape of the node that last
        answered, because that is the one a request would go to next.
        """
        index = self._active_node().endpoint_index
        return _DIALECTS[index] if index is not None else None

    def node_states(self) -> tuple[CobaltNodeState, ...]:
        """Every node as a report reads it — which is what `/doctor` and `/blocks` print.

        Data, not prose: the wording belongs to the translation layer (the admin
        reading it may be doing so in either language), and the addresses belong to
        the report, which is the only place an operator can see *which* node is out.
        """
        active = self._active_node()
        return tuple(
            CobaltNodeState(
                url=node.url,
                dialect=(
                    _DIALECTS[node.endpoint_index]
                    if node.endpoint_index is not None
                    else None
                ),
                quarantined=node.quarantined,
                reason=node.quarantine_reason,
                active=node is active,
            )
            for node in self._nodes
            if node.url
        )

    def pool_label(self) -> str:
        """The pool as one log line: the primary, then how many nodes back it up.

        Log-facing and structural on purpose (the last node is the public instance,
        and an operator should be able to see that in a single line at startup).
        """
        if not self.base_urls:
            return ""
        extra = len(self.base_urls) - 1
        if not extra:
            return self.base_url
        return f"{self.base_url} (+{extra} more)"

    def _active_node(self) -> _Node:
        """The node that answered last, or the primary while nothing has yet."""
        for node in self._nodes:
            if node.url and node.url == self._active_url:
                return node
        return self._primary

    def _attempt_order(self) -> list[_Node]:
        """Who to ask, in order: the node that answered last, then the rest.

        Quarantined nodes are skipped — that is what a quarantine is *for*. The
        sticky-first rule matters because failover is usually about one service and
        not the whole pool: an embedded instance that cannot serve YouTube (no
        session, no cookies) must not cost a failed request on every later link once
        a node that *can* serve it has been found.
        """
        fresh = [node for node in self._nodes if node.url and not node.quarantined]
        active = self._active_node()
        if active in fresh:
            return [active, *(node for node in fresh if node is not active)]
        return fresh

    def _quarantine(self, node: _Node, error: CobaltError, seconds: float = QUARANTINE_S) -> None:
        """Stop asking an instance that just failed as an instance."""
        if not error.instance:
            return  # a private video must not silence a working fallback
        node.quarantine_until = time.monotonic() + seconds
        node.quarantine_reason = f"{error.code}: {error.message}"
        self._last_quarantine = node.quarantine_reason
        logger.warning(
            "cobalt %s is being left alone for %.0fs — %s",
            node.url,
            seconds,
            node.quarantine_reason,
        )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "telegram-downloader-bot (cobalt fallback)",
        }
        if self.api_key:
            headers["Authorization"] = f"Api-Key {self.api_key}"
        return headers

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the session — only if this service created it."""
        session, self._session = self._session, None
        if session is not None and self._owns_session:
            with contextlib.suppress(Exception):
                await session.close()

    async def __aenter__(self) -> CobaltService:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def server_start_time(self) -> float | None:
        """When the instance started, in epoch seconds — or ``None``.

        Cobalt reports it on its root endpoint (``cobalt.startTime``, in
        milliseconds), and it is the only way to answer "has it loaded the cookie
        file we just wrote?" without reaching for the Docker socket: the file is
        read once, at startup. Never raises — a report must not fail over a
        nice-to-have.
        """
        if not self.enabled:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=min(self.timeout_s, 10.0))
            async with self._http().get(
                f"{self.base_url}/", headers=self._headers(), timeout=timeout
            ) as response:
                if response.status >= 400:
                    return None
                body = await response.json(content_type=None)
        except Exception:  # noqa: BLE001 — "cannot tell" is a valid answer
            return None
        info = body.get("cobalt") if isinstance(body, dict) else None
        raw = info.get("startTime") if isinstance(info, dict) else None
        if not isinstance(raw, (int, float, str)):
            return None
        try:
            return float(raw) / 1000.0
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    async def resolve(
        self, url: str, media_format: MediaFormat, quality: object = ""
    ) -> CobaltMedia:
        """Ask the pool for a direct download link for ``url``.

        ``quality`` is the tier the user picked, in Cobalt's own vocabulary (see
        :func:`video_quality_param`): a 480p request that lands on the fallback must
        not come back as the 1080p file, or the fallback would quietly override the
        choice the menu offered.

        Per instance, each known API shape is tried until one answers as an API; the
        shape that worked is remembered, so this is only ever expensive once per
        process. An instance that fails as an *instance* is quarantined (see
        ``QUARANTINE_S``) and the *next node is asked* — that is the difference
        between a fallback and a second copy of the same failure. A link that the
        instance answered about (a private video) is raised immediately: no other
        node will disagree about a deleted video, and rotating on it would triple
        the work and hide the real cause.
        """
        if not self.enabled:
            raise CobaltError("DISABLED", "نشانی کوبالت تنظیم نشده است (COBALT_API_URL خالی است).")
        nodes = self._attempt_order()
        if not nodes:
            raise CobaltError(
                "UNREACHABLE",
                f"همهٔ نمونه‌های کوبالت موقتاً کنار گذاشته شده‌اند ({self.quarantine_reason})",
                instance=True,
            )

        last: CobaltError | None = None
        tried = 0
        for node in nodes:
            tried += 1
            try:
                media = await self._resolve_on(node, url, media_format, quality)
            except CobaltError as exc:
                if not exc.instance:
                    raise  # about the *link*: rotating would only repeat the answer
                last = exc
                if tried < len(nodes):
                    logger.warning(
                        "cobalt node %s could not serve it (%s) — trying the next of %d",
                        node.url,
                        exc.code,
                        len(nodes) - tried,
                    )
                continue
            self._active_url = node.url
            return media

        error = last or CobaltError("BAD_RESPONSE", "پاسخ نمونهٔ کوبالت خوانده نشد.", instance=True)
        if tried > 1:
            # Worth one clause: an operator seeing "unreachable" needs to know the
            # pool was exhausted, not that their one address was mistyped.
            error = CobaltError(
                error.code, f"{error.message} (روی هر {tried} نمونه امتحان شد)", instance=True
            )
        raise error

    async def _resolve_on(
        self,
        node: _Node,
        url: str,
        media_format: MediaFormat,
        quality: object,
    ) -> CobaltMedia:
        """One instance's turn: its remembered dialect first, then the other shape."""
        last: CobaltError | None = None
        for index in self._endpoint_order(node):
            path, build = _ENDPOINTS[index]
            try:
                status, body = await self._post(node, path, build(url, media_format, quality))
            except CobaltError as exc:
                self._quarantine(node, exc)
                raise
            if _is_wrong_shape(status, body):
                # Right instance, wrong dialect (or a retired endpoint). Try the
                # other one; this is not a reason to tell the user anything.
                logger.info(
                    "cobalt %s does not speak the %r shape (%s) — trying the other one",
                    node.url,
                    path or "/",
                    _body_text(body)[:120],
                )
                last = CobaltError(
                    "BAD_RESPONSE",
                    f"نمونه با شکل {path or '/'} پاسخ نداد: {_body_text(body)[:160]}",
                    instance=True,
                )
                continue
            # Remembered as soon as the shape is *recognised*, not when it
            # succeeds: an instance answering `error.api.auth.jwt.missing` in the
            # v10 shape has still told us which dialect it speaks — and "my
            # instance is v10 and wants a key" is exactly what the report is read
            # for. (It also stops the next resolve from re-asking v7.)
            node.endpoint_index = index
            self._active_url = node.url
            try:
                media = _parse_response(url, status, body)
            except CobaltError as exc:
                self._quarantine(node, exc)
                raise
            return media

        error = last or CobaltError("BAD_RESPONSE", "پاسخ نمونهٔ کوبالت خوانده نشد.", instance=True)
        self._quarantine(node, error)
        raise error

    @staticmethod
    def _endpoint_order(node: _Node) -> list[int]:
        """The shapes to try: the one that worked last time first, then the rest."""
        if node.endpoint_index is None:
            return list(range(len(_ENDPOINTS)))
        return [node.endpoint_index] + [
            index for index in range(len(_ENDPOINTS)) if index != node.endpoint_index
        ]

    async def _post(self, node: _Node, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        """One resolve request. Every transport trouble becomes a CobaltError."""
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_s)
            async with self._http().post(
                f"{node.url}{path}",
                json=payload,
                headers=self._headers(),
                proxy=self.proxy or None,
                timeout=timeout,
            ) as response:
                return response.status, await _read_json(response)
        except asyncio.TimeoutError:
            raise CobaltError(
                "TIMEOUT", f"پاسخ نمونهٔ کوبالت در {self.timeout_s:.0f} ثانیه نرسید.", instance=True
            ) from None
        except aiohttp.ClientError as exc:
            raise CobaltError(
                "UNREACHABLE", f"نمونهٔ کوبالت در دسترس نبود: {exc}", instance=True
            ) from exc
        except OSError as exc:  # DNS, TLS, proxy trouble
            raise CobaltError(
                "UNREACHABLE", f"اتصال به نمونهٔ کوبالت برقرار نشد: {exc}", instance=True
            ) from exc

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download_all(
        self,
        media: CobaltMedia,
        target_dir: Path,
        *,
        max_bytes: int,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[Path]:
        """Stream every item of ``media`` into ``target_dir``, in Cobalt's order.

        One file for an ordinary download, several for a ``picker`` (a photo album or
        an instagram carousel) — and the order is the post's own, so a gallery reads
        the way its author published it. ``max_bytes`` is enforced per file as it
        streams; what the *set* may weigh is the caller's business.

        ``progress_hook`` gets yt-dlp-shaped updates, so the worker's existing
        throttle and status message work unchanged. The job directory is removed
        again if *any* item fails, exactly like the yt-dlp path does — half a
        gallery is not the post the user asked for.
        """
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            return [
                await self._download_into(part, target_dir, max_bytes, progress_hook)
                for part in media.items
            ]
        except BaseException:
            _remove_dir(target_dir)
            raise

    async def download(
        self,
        media: CobaltMedia,
        target_dir: Path,
        *,
        max_bytes: int,
        progress_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        """The single-file entry point: the first (usually only) item of ``media``.

        A ``picker`` has more than one — callers that can send a set want
        :meth:`download_all` instead of quietly losing the rest.
        """
        return (
            await self.download_all(
                media, target_dir, max_bytes=max_bytes, progress_hook=progress_hook
            )
        )[0]

    async def _download_into(
        self,
        part: CobaltPart,
        target_dir: Path,
        max_bytes: int,
        progress_hook: Callable[[dict[str, Any]], None] | None,
    ) -> Path:
        timeout = aiohttp.ClientTimeout(total=self.download_timeout_s)
        try:
            request = self._http().get(
                part.url,
                headers={"User-Agent": self._headers()["User-Agent"]},
                proxy=self.proxy or None,
                timeout=timeout,
            )
            async with request as response:
                if response.status >= 400:
                    raise CobaltError(
                        "UNREACHABLE",
                        f"لینک دانلود کوبالت پاسخ {response.status} داد (منقضی شده؟)",
                    )
                total = part.size_bytes or _as_int(response.headers.get("Content-Length"))
                if total and total > max_bytes:
                    raise CobaltError("TOO_LARGE", f"فایل کوبالت {total} بایت است (سقف: {max_bytes}).")
                path = _unique_path(target_dir, _pick_filename(part, response.headers))
                written = 0
                with path.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(_READ_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise CobaltError(
                                "TOO_LARGE", f"دانلود کوبالت از سقف {max_bytes} بایت گذشت."
                            )
                        # The loop must stay free; a disk write is still I/O.
                        await asyncio.to_thread(handle.write, chunk)
                        if progress_hook is not None:
                            _report(progress_hook, written, total or None)
                if written == 0:
                    raise CobaltError("NO_MEDIA", "لینک کوبالت فایل خالی برگرداند.")
        except asyncio.TimeoutError:
            raise CobaltError(
                "TIMEOUT", f"دانلود از کوبالت در {self.download_timeout_s:.0f} ثانیه تمام نشد."
            ) from None
        except aiohttp.ClientError as exc:
            raise CobaltError("UNREACHABLE", f"دانلود از کوبالت قطع شد: {exc}") from exc
        except OSError as exc:
            raise CobaltError("UNREACHABLE", f"نوشتن فایل کوبالت ممکن نشد: {exc}") from exc
        if progress_hook is not None:
            _report(progress_hook, written, written)
        return path


# ------------------------------------------------------------------
# Response handling (module level: no state, trivially testable)
# ------------------------------------------------------------------

async def _read_json(response: aiohttp.ClientResponse) -> Any:
    """Parse the body as JSON, tolerating a text error page."""
    with contextlib.suppress(Exception):
        return await response.json(content_type=None)
    return {}


def _is_wrong_shape(status: int, body: Any) -> bool:
    """Whether the answer means "wrong dialect/path", not "this link failed".

    Two shapes exist in the wild (see ``LEGACY_RESOLVE_PATH``) and two answers give
    it away: the retired v7 endpoint says so in words, and a v10+ instance rejects
    the renamed fields. A *link* error (unknown site, private video) is not this, so
    it is never replayed against the other shelf.
    """
    if status in (404, 405):  # the path itself is gone
        return True
    if status != 400:
        return False
    text = _body_text(body).lower()
    return any(hint in text for hint in (*_SCHEMA_HINTS, *_RETIRED_HINTS))


def _picker_parts(body: Any) -> tuple[CobaltPart, ...]:
    """The items of a ``picker`` answer, repaired and named like any other link.

    Photos, videos and gifs arrive in one list and the order is the post's own, so
    it is preserved — a gallery is read in the order the author published it.
    """
    parts: list[CobaltPart] = []
    for item in body.get("picker") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("url")
        if not isinstance(raw, str) or not raw.strip():
            continue
        url = _normalize_download_url(raw)
        parts.append(
            CobaltPart(
                url=url,
                filename=item.get("filename") if isinstance(item.get("filename"), str) else None,
                kind=str(item.get("type") or "").lower(),
            )
        )
    return tuple(parts)


def _parse_response(url: str, status: int, body: Any) -> CobaltMedia:
    """Turn one Cobalt response into a download, or into the reason there is none."""
    if status in (401, 403):
        raise CobaltError(
            "REFUSED",
            f"نمونهٔ کوبالت درخواست را رد کرد ({status}) — کلید لازم است؟",
            instance=True,
            needs_auth=True,
        )
    if status == 429:
        raise CobaltError("REFUSED", "نمونهٔ کوبالت محدودیت نرخ داد (429).", instance=True)
    if not isinstance(body, dict):
        raise CobaltError(
            "BAD_RESPONSE", f"پاسخ نمونهٔ کوبالت خوانده نشد ({status}): {_body_text(body)[:120]}"
        )
    if status >= 400:
        # 5xx is the instance; a 4xx that names a content problem is the link.
        code = _error_code(body)
        raise CobaltError(
            "ERROR",
            f"نمونهٔ کوبالت خطا داد ({status}): {_hint(body)}",
            instance=status >= 500 or code in _INSTANCE_ERROR_CODES,
            needs_auth=code in _AUTH_ERROR_CODES,
            upstream=code,
        )

    kind = str(body.get("status") or "").lower()
    if kind == "picker":
        # A multi-media post: Cobalt hands over the *list* (which is what an image
        # post with more than one photo is), and each item is fetched separately.
        parts = _picker_parts(body)
        if not parts:
            raise CobaltError("NO_MEDIA", f"کوبالت برای این لینک رسانه‌ای برنگرداند (picker خالی) — {url}")
        logger.info(
            "cobalt answered with a picker of %s item(s) for %s (%s)",
            len(parts),
            url,
            ", ".join(sorted({part.kind or "?" for part in parts})),
        )
        return CobaltMedia(parts=parts)
    if kind == "error" or isinstance(body.get("error"), dict):
        code = _error_code(body)
        raise CobaltError(
            "ERROR",
            f"کوبالت نتوانست این لینک را بگیرد: {_hint(body)}",
            instance=code in _INSTANCE_ERROR_CODES,
            needs_auth=code in _AUTH_ERROR_CODES,
            upstream=code,
        )

    link = body.get("url")
    if not isinstance(link, str) or not link.strip():
        raise CobaltError(
            "BAD_RESPONSE",
            f"پاسخ کوبالت لینک دانلود نداشت (status={kind or 'نامعلوم'}) برای {url}",
        )
    raw = link.strip()
    repaired = _normalize_download_url(raw)
    if repaired != raw:
        # The upstream's typo, not an outage — and one log line is the difference
        # between "the fallback is broken" and "this service writes bad URLs".
        logger.info(
            "cobalt returned a malformed download URL (%s) — repaired to %s",
            raw[:90],
            repaired[:90],
        )
    filename = body.get("filename") if isinstance(body.get("filename"), str) else None
    return CobaltMedia(url=repaired, filename=filename, size_bytes=_as_int(body.get("size")))


def url_is_fetchable(url: str) -> bool:
    """Whether a resolved link is one an HTTP client can actually fetch.

    The resolved URL *is* the fallback's deliverable, and it comes from a third
    party — so it is worth asserting at the edges (``smoke``/``boot_check``) rather
    than discovering inside a transfer, where a stranger's typo looks like an
    outage. Deliberately strict: it judges what it is given, so a mangled link that
    slipped past the repair below is reported instead of being silently excused.
    """
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_download_url(url: str) -> str:
    """Repair the URL shapes services do hand out, so a typo is not an outage.

    Cobalt passes a service's link through untouched, and services are not always
    careful with it: streamable's own API answers **every** video with a doubled
    scheme (``https:https://cdn-cf-west…``), and protocol-relative links
    (``//cdn/…``, an old media-site habit) come through the same way. Both are one
    edit away from a working download — and the download is the whole point of the
    fallback, so it is repaired here instead of being reported as a failure.
    """
    target = url.strip()
    while True:
        scheme, colon, rest = target.partition(":")
        if not colon or not scheme.isalpha():
            break
        head, sep, tail = rest.partition(":")
        # Only a *repeated* scheme is stripped: `https:https://host` yes,
        # `https://host/path://x` no (that is a working URL whose path has a colon).
        if not sep or not head.isalpha() or not tail.startswith("//"):
            break
        target = rest
    if target.startswith("//"):
        # No scheme of its own: these are served over https wherever they appear.
        return f"https:{target}"
    return target


def _as_int(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _filename_from_disposition(header: str | None) -> str | None:
    """``attachment; filename*=UTF-8''X`` → ``X`` (quoted and RFC 5987 forms)."""
    if not header:
        return None
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() not in {"filename", "filename*"}:
            continue
        name = value.strip().strip('"').strip("'")
        if "''" in name:  # RFC 5987: charset'lang'value
            name = name.split("''", 1)[1]
        if name:
            with contextlib.suppress(Exception):
                return unquote(name)
            return name
    return None


def _unique_path(directory: Path, name: str) -> Path:
    """A free name inside ``directory``: two items of one post must both survive.

    Services hand out repeated names for a gallery (``twitter_1.jpg`` from two
    different retries, or one name for every item), and overwriting the first photo
    with the second would look exactly like a download that worked.
    """
    path = directory / name
    if not path.exists():
        return path
    for index in range(2, 100):
        candidate = directory / f"{path.stem}-{index}{path.suffix}"
        if not candidate.exists():
            return candidate
    return path  # pragma: no cover — a hundred identical names in one job


def _pick_filename(part: CobaltPart, headers: Any) -> str:
    """Name the file from what Cobalt, the transport or the URL said — in that order.

    Cobalt names files ``Title [id].ext``, which is the only title-like text the
    fallback ever gives us, so it is worth keeping rather than inventing a name.
    """
    content_type = str(headers.get("Content-Type", "")).partition(";")[0].strip().lower()
    for candidate in (
        part.filename,
        _filename_from_disposition(headers.get("Content-Disposition")),
        unquote(Path(urlparse(part.url).path).name),
    ):
        if candidate:
            name = sanitize_filename(candidate)
            if "." in name:
                return name
            if extension := _CONTENT_TYPE_EXT.get(content_type):
                return f"{name}.{extension}"
            return name
    extension = _CONTENT_TYPE_EXT.get(content_type) or "mp4"
    return f"cobalt-fallback.{extension}"


def _report(
    hook: Callable[[dict[str, Any]], None], written: int, total: int | None
) -> None:
    """Feed the hook the same shape yt-dlp does (the worker's editor expects it)."""
    try:
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": written,
                "total_bytes": total,
                "total_bytes_estimate": total,
            }
        )
    except Exception:  # a progress message must never break a transfer
        logger.exception("cobalt: progress hook raised")


def _remove_dir(directory: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(directory, ignore_errors=True)
