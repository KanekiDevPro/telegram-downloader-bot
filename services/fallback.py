"""Hand a blocked link to the fallback extractor, and get an ordinary result back.

The choice of when to fall back matters more than the mechanics. Only a dead end
for the primary engine is handed over: a block (the site refused this address, or
the jar we sent it), yt-dlp's own DRM verdict (it will not touch that site at
all), and a post with no video in it (an image post — where the photos *are* the
content, and the other engine's business). Each can differ elsewhere, which is the
whole premise of a second engine. A private video, a live stream or a playlist is
the same answer from anywhere, so retrying those through Cobalt would only cost
the user another wait — and a network round trip into the bargain.

What comes back is a plain :class:`DownloadResult`, so the worker's upload,
caching and status messages stay exactly as they were: the user cannot tell which
engine produced the file, and that is the point.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import asyncpg

from core import database
from core.utils import MediaFormat
from services.cobalt import CobaltService
from services.extractor import (
    BLOCK_EXTRACTION_CODES,
    DRM_PROTECTED_CODE,
    IMAGE_ONLY,
    DownloadResult,
    ExtractionError,
    MediaInfo,
    url_host,
)

logger = logging.getLogger(__name__)

#: The failures worth another engine. The blocks are "the site refused *us*", where
#: a request from another address behaves differently; DRM is "this engine will not
#: do that site at all", where another engine (Cobalt reaches some of them through a
#: different service) still might; and an image post is "there is no video here",
#: where the other engine simply does something this one cannot. Every other code
#: (private, geo, live, playlist, ffmpeg) gets the same answer from anyone, and
#: burning a fallback attempt on them teaches the user nothing.
FALLBACK_ERROR_CODES: frozenset[str] = BLOCK_EXTRACTION_CODES | {
    DRM_PROTECTED_CODE,
    IMAGE_ONLY,
}

#: Host → the platform name the caption shows. Unmapped hosts fall back to their own
#: name, which is at least true; the map exists so a short link says ``youtube``.
_PLATFORM_BY_HOST: dict[str, str] = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "youtube-nocookie.com": "youtube",
    "music.youtube.com": "youtube",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "facebook.com": "facebook",
    "fb.watch": "facebook",
    "reddit.com": "reddit",
    "vimeo.com": "vimeo",
    "soundcloud.com": "soundcloud",
    "dailymotion.com": "dailymotion",
    "twitch.tv": "twitch",
    "bilibili.com": "bilibili",
    "vk.com": "vk",
}

#: Cobalt names files ``Title [id].ext``. The bracket is Cobalt's, not the video's,
#: so it is dropped from the caption but kept in the filename.
_BRACKET_SUFFIX = re.compile(r"\s*\[[^\]\s]{3,}\]\s*$")


def should_use_fallback(error: ExtractionError, service: CobaltService | None) -> bool:
    """Whether this failure is one the fallback could plausibly fix.

    ``available`` rather than ``enabled``: an instance that just failed as an
    instance (unreachable, refusing us, rate-limited) is being left alone for a
    while, and handing it the link would only add a doomed round trip to the wait
    the user is already having. They get the original diagnosis instead — which is
    what they would have had without a fallback at all.
    """
    if service is None or not service.enabled or not service.available:
        return False
    return error.code in FALLBACK_ERROR_CODES


#: Set on an error whose link has already been through *both* engines. The retry
#: wrapper reads it: a blocked IP does not un-block itself in the two seconds before
#: the next attempt, and a rate-limited instance answers the same way — so retrying
#: the pair would only spend the user's wait, not their link.
_FALLBACK_ATTEMPTED = "fallback_attempted"


def mark_fallback_attempted(error: ExtractionError) -> ExtractionError:
    """Remember that this link has now failed on the primary *and* the fallback."""
    setattr(error, _FALLBACK_ATTEMPTED, True)
    return error


def fallback_was_attempted(error: BaseException) -> bool:
    """Whether both engines were already tried for this failure."""
    return bool(getattr(error, _FALLBACK_ATTEMPTED, False))


def platform_for(url: str) -> str:
    """A readable platform name for a link (``youtu.be`` → ``youtube``)."""
    host = url_host(url)
    if host in _PLATFORM_BY_HOST:
        return _PLATFORM_BY_HOST[host]
    for known, platform in _PLATFORM_BY_HOST.items():
        if host.endswith(f".{known}"):
            return platform
    labels = host.split(".")
    if len(labels) >= 2 and labels[-2] not in {"co", "com", "org", "net"}:
        return labels[-2]
    return labels[0] if labels and labels[0] != "?" else "unknown"


def title_for(url: str, file_path: Path) -> str:
    """A caption title, from the fallback's own filename when it has one.

    Cobalt's ``nerd`` naming is the only title-like text the fallback ever returns,
    so it is used rather than invented. When the file is a generic one (a bare
    ``cobalt-fallback.mp4``), the link is the more honest label.
    """
    stem = file_path.stem
    if not stem.startswith("cobalt-fallback"):
        cleaned = _BRACKET_SUFFIX.sub("", stem).strip()
        if cleaned:
            return cleaned
    return f"{platform_for(url)} · {url.rsplit('/', 1)[-1][:60]}"


def _media_info(url: str, file_path: Path, size_bytes: int | None) -> MediaInfo:
    size = size_bytes or (file_path.stat().st_size if file_path.exists() else None)
    return MediaInfo(
        source_url=url,
        title=title_for(url, file_path),
        platform=platform_for(url),
        webpage_url=url,
        extension=file_path.suffix.lstrip(".") or "mp4",
        thumbnail=None,  # the fallback does not report metadata; a guess would be worse
        duration=None,
        filesize_approx=size,
        is_live=False,
    )


async def fetch(
    service: CobaltService,
    url: str,
    media_format: MediaFormat,
    *,
    quality: object = "",
    download_dir: Path,
    max_bytes: int,
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> DownloadResult:
    """Resolve ``url`` through the fallback and download it into a fresh job dir.

    ``quality`` rides along to the instance so the tier the user picked survives the
    hand-over — a 480p ask must not come back as whatever the fallback considers
    best.

    Same directory shape as the yt-dlp path (``job-<id>``) so the worker's cleanup,
    the upload and the stale-job sweep need no special case. Failures raise
    :class:`CobaltError` with its own code; the caller keeps the *original* yt-dlp
    diagnosis for the user, because that one is about their link.
    """
    media = await service.resolve(url, media_format, quality)
    job_dir = download_dir / f"job-{uuid.uuid4().hex[:10]}"
    # Plural on purpose: a photo album arrives as a *set*, and dropping all but the
    # first picture would look like a working download of a post nobody asked for.
    paths = await service.download_all(
        media,
        job_dir,
        max_bytes=max_bytes,
        progress_hook=progress_hook,
    )
    file_path, extra = paths[0], tuple(paths[1:])
    logger.info(
        "fallback produced %s%s for %s (%s bytes)",
        file_path.name,
        f" + {len(extra)} more" if extra else "",
        url,
        sum(path.stat().st_size for path in paths),
    )
    return DownloadResult(
        file_path=file_path,
        info=_media_info(url, file_path, media.size_bytes),
        media_format=media_format,
        extra_paths=extra,
        quality=str(quality or ""),
    )


#: The fallback never reports a resolution, so a caption built from its result says
#: nothing about quality rather than inventing a number. ``height`` stays ``None``
#: for the same reason ``duration`` does.


def describe(error: Exception) -> str:
    """One line for the log: why the fallback did not save this link."""
    code = getattr(error, "code", error.__class__.__name__)
    message = getattr(error, "message", None) or str(error)
    return f"{code}: {message}"


def skip_reason(error: ExtractionError, service: CobaltService | None) -> str:
    """Why this failure did *not* get the fallback — or ``""`` when it did.

    Defined as the exact complement of :func:`should_use_fallback`, so the answer
    an admin reads can never disagree with the decision the worker took: whatever
    made the net un-usable is what gets written down. A failure the fallback would
    never have taken (a private video, a playlist) returns ``""`` — "the net was
    skipped" would be a lie there, and quiet is the honest answer.
    """
    if should_use_fallback(error, service):
        return ""
    if error.code not in FALLBACK_ERROR_CODES:
        return ""
    if service is None:
        return "کلاینت fallback در این اجرا ساخته نشده بود"
    if not service.enabled:
        return "خاموش بود (COBALT_API_URL خالی)"
    quarantined_for = getattr(service, "quarantine_reason", "")
    return f"قرنطینه بود ({quarantined_for})" if quarantined_for else "قرنطینه بود"


# ---------------------------------------------------------------------------
# What the net did the last time real traffic needed it
# ---------------------------------------------------------------------------

#: Where that answer is kept. The doctor's verdict (``services.doctor``) is about a
#: *probe* — whether the instance would answer; this row is about *traffic* — the
#: last blocked link that needed the fallback, and what happened to it. A quarantine
#: expires after ten minutes and a probe is only as fresh as the last /doctor, so
#: neither can answer "was the net there when it mattered?" on its own.
USE_STATE_KEY = "cobalt_last_use"

USE_USED = "used"
USE_FAILED = "failed"
USE_SKIPPED = "skipped"
USE_OUTCOMES: frozenset[str] = frozenset({USE_USED, USE_FAILED, USE_SKIPPED})

_USE_LABELS: dict[str, str] = {
    USE_USED: "✅ فایل را ساخت",
    USE_FAILED: "❌ خودش هم نشد",
    USE_SKIPPED: "⏭ رد شد (استفاده نشد)",
}


def _serialise(outcome: str, reason: str, *, now: float | None = None) -> str:
    """``epoch|outcome|reason`` — the row both writer and reader agree on."""
    return f"{now if now is not None else time.time():.0f}|{outcome}|{reason}"


@dataclass(frozen=True)
class FallbackUse:
    """The last time a blocked link needed the fallback, and what came of it.

    ``seconds`` is computed when the row is read, not written, so an old row ages
    honestly instead of claiming to be as fresh as the process that read it.
    """

    outcome: str
    seconds: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == USE_USED

    @property
    def label(self) -> str:
        return _USE_LABELS.get(self.outcome, self.outcome)

    def stored(self) -> str:
        """The row as it is written, at the moment it is written."""
        return _serialise(self.outcome, self.reason)

    @classmethod
    def parse(cls, value: str, *, now: float | None = None) -> FallbackUse | None:
        """Read a stored row back; ``None`` when it is not ours (or unreadable)."""
        parts = value.split("|", 2)
        if len(parts) != 3 or parts[1] not in USE_OUTCOMES:
            return None
        epoch, outcome, reason = parts
        try:
            seconds = max(0.0, (now if now is not None else time.time()) - float(epoch))
        except ValueError:
            return None
        return cls(outcome=outcome, seconds=seconds, reason=reason)


async def remember_use(
    pool: asyncpg.Pool, outcome: str, reason: str = ""
) -> None:
    """Write down what the net just did for a real link.

    Called from the worker's failure path, so it must never raise: losing this note
    is a shame, losing the user's error message over it is not acceptable.
    """
    if outcome not in USE_OUTCOMES:  # pragma: no cover — a programming mistake
        logger.error("unknown fallback outcome %r", outcome)
        return
    try:
        await database.set_state(pool, USE_STATE_KEY, _serialise(outcome, reason))
    except Exception:
        logger.exception("could not record the fallback's last use")


async def last_use(pool: asyncpg.Pool) -> FallbackUse | None:
    """Read that row back; ``None`` when the net has not been needed yet."""
    try:
        value = await database.get_state(pool, USE_STATE_KEY)
    except Exception:
        logger.exception("could not read the fallback's last use")
        return None
    return FallbackUse.parse(value) if value else None
