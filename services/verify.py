"""Delivery verification: the produced file testifies before it is captioned.

The caption is a promise — «MP3 · 320 kbps», «FLAC», «1080p» — and this module is
where the finished file keeps it honest. One ``ffprobe`` run per delivery (the
file the caption names; albums are captioned by their first file) measures what
actually exists — container, codec, bitrate, duration, sample rate, channels,
dimensions — and the measurement is compared against the *same* claim the
caption will make (``services.delivery.produced_quality_label`` reads the same
tier semantics through ``services.extractor``).

**Policy — documented on purpose.** The verification *fails closed* only on a
measured contradiction: the file says MP3 where FLAC was promised, or a «320
kbps» tier produced a file that averages well below it. When ffprobe is missing,
times out, or answers something unreadable, nothing has contradicted anything —
the run is marked "verification unavailable" in the log (once per process) and
delivery proceeds exactly as it always did. Refusing to deliver a good file
because a *diagnostic* tool is absent would punish the user for an operator's
packaging choice, and the produced-container guard in the extractor remains the
hard floor. An unrecognised codec or a missing field is likewise not a
contradiction: only what the file *says* is checked.

Tolerances are deliberately generous where the format itself is loose: encoded
bitrate is a target, not a measurement (VBR, container overhead, padding), so a
delivered average between 0.7× and 1.5× of the claimed rate is the claim kept —
an instantaneous VBR spike never fails a file. Lossless containers (FLAC, WAV)
have no bitrate knob to lie about and are verified by codec alone. Video claims
a resolution, so the measured height is what is checked (±5%, ≥16 px for coded
padding like 1088 vs 1080).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from core.utils import normalize_quality
from services.extractor import audio_bitrate, audio_is_original

logger = logging.getLogger(__name__)

#: How long one ffprobe run may take. It reads a header, not the file — anything
#: slower than this is a broken install, not a big video.
PROBE_TIMEOUT_S = 10.0

#: Measured average bitrate must sit within this factor of the claimed rate.
#: Below it the file is materially smaller than promised; above it the number on
#: the label is simply not what the encoder was asked for. VBR lives inside
#: this window by design.
BITRATE_TOLERANCE = (0.7, 1.5)


def bitrate_in_window(measured_kbps: float, nominal_kbps: int) -> bool:
    """Whether a measured average rate keeps a nominal tier's promise.

    The one place the tolerance is applied, so no check can grow its own magic
    numbers. The window is inclusive on both edges and deliberately the same for
    every nominal lossy tier: it is sized for the loosest of them (opus is VBR
    with a *target*; mp3/aac re-encodes are near-CBR and variance sits well
    inside), and a wider per-codec window would only weaken the tighter codecs'
    existing protection. "Materially below" — a 192 kbps file under a 320 kbps
    label — lands outside it and contradicts the claim; a VBR dip or container
    overhead inside it does not.
    """
    low, high = BITRATE_TOLERANCE
    return low * nominal_kbps <= measured_kbps <= high * nominal_kbps


def is_source_upscale(nominal_kbps: int, source_kbps: int | None) -> bool | None:
    """Would this encoding target exceed the source's own lossy rate? ``None`` = unknown.

    A *local observation*, deliberately wired to no contract: a 320 kbps MP3
    from a ~130 kbps AAC is a technically successful delivery (the requested
    encoding target was produced) that simply carries no restored information —
    it is neither a delivery failure nor a user-facing state, so nothing here
    may become one. Three answers on purpose: ``True`` (upscale), ``False``
    (within the source), and ``None`` when the source rate is unknown — an
    absent source rate proves nothing in *either* direction, and neither
    conclusion may be invented from it.
    """
    if not nominal_kbps or nominal_kbps <= 0 or not source_kbps or source_kbps <= 0:
        return None
    return nominal_kbps > source_kbps

#: Coded heights pad to macroblock boundaries (1080 is often stored as 1088).
HEIGHT_TOLERANCE_PX = 16
HEIGHT_TOLERANCE_RATIO = 0.05

#: Container extension per encoder codec (the file a tier's conversion must
#: leave behind — the extractor's guard checks the same contract).
_CODEC_EXT: dict[str, str] = {
    "mp3": ".mp3",
    "m4a": ".m4a",
    "aac": ".m4a",
    "opus": ".opus",
    "wav": ".wav",
    "flac": ".flac",
}

#: Which codecs each container may honestly hold. ``pcm_*`` is a family (WAV can
#: carry several widths), spelled as a prefix below.
_CONTAINER_CODECS: dict[str, frozenset[str]] = {
    ".mp3": frozenset({"mp3"}),
    ".m4a": frozenset({"aac", "alac"}),
    ".aac": frozenset({"aac", "alac"}),
    ".opus": frozenset({"opus", "vorbis"}),
    ".ogg": frozenset({"opus", "vorbis"}),
    ".flac": frozenset({"flac"}),
    ".wav": frozenset(),  # family: pcm_*
}
_PCM_PREFIX = "pcm_"

#: One "verification unavailable" log per process — a deployment without
#: ffprobe hears about it once, not once per download.
_unavailable_logged = False


@dataclass(frozen=True)
class MediaFacts:
    """What ffprobe could measure about the finished file — never guessed.

    A field is ``None`` when the format does not carry it (or ffprobe did not
    report it); every check below treats "not reported" as "cannot contradict".
    """

    format_name: str = ""
    codec: str = ""
    bitrate_bps: int | None = None
    duration_s: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    width: int | None = None
    height: int | None = None


async def probe_media(path: Path) -> MediaFacts | None:
    """Measure the file with ffprobe. ``None`` when nothing could be learned.

    Missing binary, timeout, non-zero exit and malformed output all mean the
    same thing here: verification is unavailable (see the policy above), and the
    caller must treat ``None`` as "carry on", never as "this file is wrong".
    """
    binary = shutil.which("ffprobe")
    if binary is None:
        _note_unavailable("ffprobe is not installed")
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "--",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(), timeout=PROBE_TIMEOUT_S
        )
    except FileNotFoundError:
        _note_unavailable("ffprobe disappeared from PATH")
        return None
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        _note_unavailable(f"ffprobe timed out on {path.name}")
        return None
    except OSError:
        _note_unavailable(f"ffprobe could not be started for {path.name}")
        return None
    if process.returncode != 0:
        _note_unavailable(f"ffprobe exited {process.returncode} on {path.name}")
        return None
    return _parse_probe(stdout)


def _parse_probe(raw: bytes) -> MediaFacts | None:
    """Turn ffprobe's JSON into facts — ``None`` when it is not the shape we know.

    A malformed answer is an unreadable answer, not a contradiction.
    """
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
        streams = [
            stream
            for stream in data.get("streams", ())
            if isinstance(stream, dict) and stream.get("codec_type") in ("audio", "video")
        ]
        container = data.get("format") or {}
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
        _note_unavailable("ffprobe answered something unreadable")
        return None
    if not streams:
        _note_unavailable("ffprobe reported no audio or video stream")
        return None
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    subject = audio if audio is not None else video
    assert subject is not None  # the list is non-empty by construction
    return MediaFacts(
        format_name=str(container.get("format_name") or ""),
        codec=str(subject.get("codec_name") or "").lower(),
        bitrate_bps=_int_or_none(subject.get("bit_rate")) or _int_or_none(container.get("bit_rate")),
        duration_s=_float_or_none(container.get("duration")),
        sample_rate=_int_or_none(subject.get("sample_rate")),
        channels=_int_or_none(subject.get("channels")),
        width=_int_or_none((video or {}).get("width")),
        height=_int_or_none((video or {}).get("height")),
    )


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _note_unavailable(reason: str) -> None:
    """Hear about a broken verification setup once, loudly enough to act on."""
    global _unavailable_logged
    if _unavailable_logged:
        logger.debug("delivery verification unavailable: %s", reason)
        return
    _unavailable_logged = True
    logger.warning(
        "delivery verification unavailable (%s) — produced files will be "
        "captioned from the request's metadata as before",
        reason,
    )


def check_produced(
    facts: MediaFacts,
    *,
    media_format: str,
    quality: str,
    suffix: str,
    produced_p: object = None,
) -> str | None:
    """Compare a measurement against the caption's claim.

    Returns a short technical description of the contradiction (for the log —
    never for the chat), or ``None`` when the file keeps the claim *or* nothing
    could be contradicted. Same inputs as
    ``services.delivery.produced_quality_label``, so what is checked is exactly
    what will be said.
    """
    suffix = (suffix or "").lower()
    if media_format != "audio":
        return _check_video(facts, produced_p)
    return _check_audio(facts, quality=quality, suffix=suffix)


def _check_audio(facts: MediaFacts, *, quality: str, suffix: str) -> str | None:
    expected = _container_claims(suffix)
    if expected is not None and facts.codec:
        codecs, family = expected
        if not (family and facts.codec.startswith(_PCM_PREFIX)) and facts.codec not in codecs:
            return f"codec: container {suffix} claims one of {sorted(codecs)}, measured {facts.codec!r}"
    tier = normalize_quality(quality, "audio")
    if audio_is_original(tier):
        # A copied stream is the site's own file — its rate is nobody's promise.
        return None
    kbps = audio_bitrate(tier)
    if not kbps or suffix != _CODEC_EXT.get(tier.split(".")[0]):
        # The label names no bitrate for this file (lossless, or a container the
        # tier did not ask for), so there is no rate to contradict.
        return None
    if facts.bitrate_bps is None or facts.bitrate_bps <= 0:
        return None
    measured_kbps = facts.bitrate_bps / 1000
    if not bitrate_in_window(measured_kbps, kbps):
        return f"bitrate: claimed {kbps} kbps, measured {measured_kbps:.0f} kbps (avg)"
    return None


def _container_claims(suffix: str) -> tuple[frozenset[str], bool] | None:
    """The codecs a container may hold — ``(codecs, is_pcm_family)``.

    ``None`` for a container we have no opinion about: an unknown suffix cannot
    contradict anything.
    """
    codecs = _CONTAINER_CODECS.get(suffix)
    if codecs is None:
        return None
    return codecs, suffix == ".wav"


def _check_video(facts: MediaFacts, produced_p: object) -> str | None:
    try:
        expected_height = int(str(produced_p).strip())
    except (TypeError, ValueError):
        return None  # the caption claims no resolution — nothing to check
    if facts.height is None or facts.height <= 0:
        return None  # not reported cannot contradict
    tolerance = max(HEIGHT_TOLERANCE_PX, expected_height * HEIGHT_TOLERANCE_RATIO)
    if abs(facts.height - expected_height) > tolerance:
        return f"resolution: claimed {expected_height}p, measured height {facts.height}"
    return None


async def verify_produced(
    path: Path,
    *,
    media_format: str,
    quality: str,
    suffix: str = "",
    produced_p: object = None,
) -> str | None:
    """Probe the produced file and check it against the caption's claim.

    One call per delivery, on the file the caption names. Returns the mismatch
    description (log it, fail the delivery) or ``None`` to proceed — including
    when verification was unavailable.
    """
    facts = await probe_media(path)
    if facts is None:
        return None
    mismatch = check_produced(
        facts,
        media_format=media_format,
        quality=quality,
        suffix=suffix or path.suffix,
        produced_p=produced_p,
    )
    if mismatch:
        logger.error("delivery verification failed for %s: %s", path.name, mismatch)
    return mismatch


__all__ = [
    "BITRATE_TOLERANCE",
    "check_produced",
    "MediaFacts",
    "probe_media",
    "PROBE_TIMEOUT_S",
    "verify_produced",
]
# ``is_source_upscale`` is deliberately *not* exported: it is a local
# observation helper with no production caller and no result contract to grow —
# tests reach it through the module (``verify.is_source_upscale``).
