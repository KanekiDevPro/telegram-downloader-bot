"""Centralized audio quality engine: rank candidates, plan outputs, prove lossless.

The engine reads *normalized* candidate data only — identity confidence,
verified flags, codec facts — and never names a provider: provider-specific
behavior lives in :mod:`services.providers`, and this module would not know a
new Hi-Fi source from the old ones. Ranking is deliberately not bitrate-only:
confidence dominates every audio fact, because the wrong song in FLAC is still
the wrong song.

True Lossless is a strict conjunction, checked in one place
(:meth:`QualityEngine.plan` and :func:`finalize_output` share it): a verified
lossless source, a lossless output, and no lossy step anywhere. Anything else
asking for lossless raises :class:`FakeLosslessRejected` instead of producing
a file that lies about itself.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from core.utils import normalize_quality
from services.audio_models import (
    AudioCandidate,
    AudioMode,
    OutputAudio,
    Provenance,
    SourceAudio,
    match_method_rank,
)
from services.extractor import audio_bitrate
from services.verify import MediaFacts

logger = logging.getLogger(__name__)

#: The cache qualifier a verified-lossless delivery is stored under (see
#: services/cache.py): lossy rows under the bare tier can never satisfy it.
LOSSLESS_QUALIFIER = "verified-lossless"

#: Source-codec preference for otherwise tied candidates, best first. Data, not
#: policy about any provider: a lossless codec outranks a lossy one, and among
#: lossy codecs the ranking follows transparency at typical served rates.
#: ``m4a`` is deliberately absent: it is a container (and the application's
#: no-conversion tier label), never a codec — ranking it here would confuse the
#: box the sound arrives in with the sound itself.
CODEC_RANK: tuple[str, ...] = ("flac", "alac", "wav", "opus", "aac", "mp3")

#: Codecs whose bytes are mathematically lossless (WAV's ``pcm_*`` family included).
LOSSLESS_CODECS: frozenset[str] = frozenset({"flac", "alac", "wav"})
_PCM_PREFIX = "pcm_"

#: Containers that can only hold lossless sound (matched case-insensitively,
#: with or without the leading dot).
LOSSLESS_CONTAINERS: frozenset[str] = frozenset({".flac", ".wav", "flac", "wav"})


class FakeLosslessRejected(ValueError):
    """A lossless output was requested from a source that cannot prove it.

    Raised instead of producing the file: a lossy stream poured into FLAC is a
    bigger file, not better sound, and the pipeline must say so rather than
    ship it.
    """


def _codec_rank(codec: Optional[str]) -> int:
    if not codec:
        return -1
    try:
        return CODEC_RANK.index(codec.lower())
    except ValueError:
        return -1


def _is_lossless_codec(codec: Optional[str]) -> bool:
    if not codec:
        return False
    name = codec.lower()
    return name in LOSSLESS_CODECS or name.startswith(_PCM_PREFIX)


def _is_lossless_container(container: Optional[str]) -> bool:
    if not container:
        return False
    return container.lower() in LOSSLESS_CONTAINERS


def _same_container(first: Optional[str], second: Optional[str]) -> bool:
    """Whether two container spellings name the same box (dot-insensitive)."""
    if not first or not second:
        return False
    return first.lower().lstrip(".") == second.lower().lstrip(".")


def _lossless_shape(source: SourceAudio, container: Optional[str]) -> Provenance:
    """Native or remux for a verified lossless, untranscoded delivery.

    Same box as the source served: native. A different lossless box with no
    lossy step anywhere: a container-only remux, still every original bit.
    Unknown on either side stays native — changing the label on a guess would
    be the same lie the gate exists to refuse.
    """
    if source.container and container and not _same_container(source.container, container):
        return Provenance.LOSSLESS_REMUX
    return Provenance.LOSSLESS_NATIVE


def _rank_key(
    candidate: AudioCandidate,
) -> tuple[int, float, int, int, int, int, int, int, str, str]:
    """Best first: identity signal, then confidence, then audio facts.

    The categorical match tier dominates everything (the platform's own id,
    then an exact recording-id match, then any metadata evidence); confidence
    orders only within a tier, so the hierarchy never depends on float gaps.
    Verified lossless dominates unverified claims next; only then do codec,
    depth, rate and bitrate break ties. ``None`` facts sort lowest — unknown
    is never promoted. The provider identity closes the key so exact ties
    resolve the same way whatever order the candidates arrived in.
    """
    verified = 1 if (candidate.provider_verified_lossless and candidate.is_lossless) else 0
    return (
        match_method_rank(candidate.match_method),
        float(candidate.match_confidence or 0.0),
        verified,
        1 if candidate.is_lossless else 0,
        _codec_rank(candidate.codec),
        int(candidate.bit_depth or 0),
        int(candidate.sample_rate or 0),
        int(candidate.bitrate_bps or 0),
        candidate.provider_name,
        candidate.provider_track_id,
    )


class QualityEngine:
    """Rank normalized candidates and plan honest outputs for them."""

    @staticmethod
    def rank(candidates: Sequence[AudioCandidate]) -> list[AudioCandidate]:
        """Best candidate first; the input order never matters, only the facts."""
        return sorted(candidates, key=_rank_key, reverse=True)

    @staticmethod
    def select(candidates: Sequence[AudioCandidate]) -> AudioCandidate:
        """The single candidate to serve — the ranking's head, never bitrate-only."""
        ranked = QualityEngine.rank(candidates)
        if not ranked:
            raise ValueError("no audio candidates to select from")
        return ranked[0]

    @staticmethod
    def plan(source: SourceAudio, mode: AudioMode) -> OutputAudio:
        """The honest output for ``source`` under ``mode`` — or a refusal.

        ORIGINAL copies the source's own stream (codec and container ride
        through). MP3_320 intentionally transcodes (provenance kept, never
        lossless). TRUE_LOSSLESS requires the strict conjunction and raises
        :class:`FakeLosslessRejected` for anything else.
        """
        if mode == AudioMode.ORIGINAL:
            lossless = bool(
                source.provider_verified_lossless and source.is_lossless
            )
            return OutputAudio(
                codec=source.codec,
                container=source.container,
                bitrate_bps=source.bitrate_bps,
                sample_rate=source.sample_rate,
                bit_depth=source.bit_depth,
                channels=source.channels,
                transcoded=False,
                true_lossless=lossless,
                provenance=(
                    _lossless_shape(source, source.container)
                    if lossless
                    else (
                        Provenance.LOSSY_NATIVE
                        if not source.is_lossless
                        else Provenance.UNKNOWN
                    )
                ),
                source=source,
            )
        if mode == AudioMode.MP3_320:
            return QualityEngine.plan_transcode(source, "mp3", 320_000)
        if mode == AudioMode.TRUE_LOSSLESS:
            if not (source.provider_verified_lossless and source.is_lossless):
                logger.info(
                    "true-lossless refused: unverified or lossy source "
                    "(codec=%s container=%s)",
                    source.codec,
                    source.container,
                )
                raise FakeLosslessRejected(
                    "true lossless needs a provider-verified lossless source; "
                    "a lossy stream poured into FLAC is a bigger file, not better sound"
                )
            return OutputAudio(
                codec=source.codec,
                container=source.container,
                bitrate_bps=source.bitrate_bps,
                sample_rate=source.sample_rate,
                bit_depth=source.bit_depth,
                channels=source.channels,
                transcoded=False,
                true_lossless=True,
                provenance=_lossless_shape(source, source.container),
                source=source,
            )
        raise ValueError(f"unknown audio mode: {mode!r}")

    @staticmethod
    def plan_transcode(source: SourceAudio, codec: str, bitrate_bps: int) -> OutputAudio:
        """An intentional re-encode: the target is named, the source is kept.

        A transcode target is never lossless, whatever the source was — even a
        verified FLAC poured into MP3 is lossy output, said so plainly.
        """
        return OutputAudio(
            codec=codec,
            # Every transcode target in AUDIO_EXPORTS names its own container.
            container=f".{codec}",
            bitrate_bps=bitrate_bps,
            sample_rate=source.sample_rate,
            channels=source.channels,
            transcoded=True,
            true_lossless=False,
            provenance=Provenance.LOSSY_TRANSCODED,
            source=source,
        )

    @staticmethod
    def plan_for_tier(source: SourceAudio, tier: object) -> OutputAudio:
        """Plan any requested tier: transcodes keep their own target rate.

        ``mp3.best`` is the 320 HQ transcode; other bitrate tiers transcode to
        exactly what they name (a 128k ask is served 128k, honestly); the
        untouched stream copies; lossless containers face the strict gate.
        """
        normalized = normalize_quality(tier, "audio")
        if normalized in ("flac", "wav"):
            return QualityEngine.plan(source, AudioMode.TRUE_LOSSLESS)
        if normalized in ("best", "m4a"):
            return QualityEngine.plan(source, AudioMode.ORIGINAL)
        if normalized == "mp3.best":
            return QualityEngine.plan(source, AudioMode.MP3_320)
        bitrate = audio_bitrate(normalized)
        if bitrate:
            codec = normalized.split(".", 1)[0]
            return QualityEngine.plan_transcode(source, codec, bitrate * 1000)
        return QualityEngine.plan(source, AudioMode.ORIGINAL)


def finalize_output(
    source: SourceAudio, facts: MediaFacts | None, planned: OutputAudio
) -> OutputAudio:
    """Confirm a planned output against the ffprobe-measured file.

    The measured container/codec/rate win over the plan (the file is
    authoritative); the transcode flag and the source stay the plan's. Without
    facts nothing is invented — the plan stands, with its own honesty intact.
    """
    if facts is None:
        return planned
    codec = facts.codec or planned.codec
    container = None
    if facts.format_name:
        container = f".{facts.format_name.split(',')[0].strip()}"
    container = container or planned.container
    measured_lossless = _is_lossless_codec(codec) or _is_lossless_container(container)
    true_lossless = bool(
        source.provider_verified_lossless
        and source.is_lossless
        and measured_lossless
        and not planned.transcoded
    )
    if true_lossless:
        provenance = _lossless_shape(source, container)
    elif planned.transcoded:
        provenance = (
            Provenance.LOSSY_TRANSCODED if not measured_lossless else Provenance.UNKNOWN
        )
    elif measured_lossless:
        # Lossless bytes with no verified lossless source: a claim, not a fact.
        provenance = Provenance.UNKNOWN
    else:
        provenance = Provenance.LOSSY_NATIVE
    return OutputAudio(
        codec=codec,
        container=container,
        bitrate_bps=(
            int(facts.bitrate_bps) if facts.bitrate_bps else planned.bitrate_bps
        ),
        sample_rate=facts.sample_rate or planned.sample_rate,
        bit_depth=planned.bit_depth,
        channels=facts.channels or planned.channels,
        transcoded=planned.transcoded,
        true_lossless=true_lossless,
        provenance=provenance,
        source=source,
    )
