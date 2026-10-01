"""Audio quality domain models: identity, candidates, source/output, provenance.

Two separations this module exists to keep. Track *identity* (which song) is
not audio *retrieval* (where bytes come from): a confident match of a lossy
stream beats a doubtful match of a lossless one, and the fields for the two
never share a name. *Source* facts (what the provider served) are not *output*
facts (what the file became): a transcode target never overwrites what the
source was, and a container never proves lossless sound on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional


class Provenance(str, Enum):
    """How the delivered bytes relate to the source's sound."""

    #: A verified lossless source delivered untouched in its own container.
    LOSSLESS_NATIVE = "lossless_native"
    #: A verified lossless source moved losslessly into another container.
    LOSSLESS_REMUX = "lossless_remux"
    #: A lossy source delivered without re-encoding.
    LOSSY_NATIVE = "lossy_native"
    #: A lossy (or any) source re-encoded through a lossy step.
    LOSSY_TRANSCODED = "lossy_transcoded"
    #: Nothing verified: the safe answer when no check could tell.
    UNKNOWN = "unknown"


class AudioMode(str, Enum):
    """What the user asked the pipeline to produce."""

    #: The selected native source, no unnecessary transcoding.
    ORIGINAL = "original"
    #: An intentional transcode to MP3 320 kbps (provenance kept, never lossless).
    MP3_320 = "mp3_320"
    #: Lossless output — only from a verified lossless source, never converted up.
    TRUE_LOSSLESS = "true_lossless"


#: Identity-signal strength by match method, strongest first: the platform's
#: own id outranks an exact recording-id match, which outranks any metadata
#: evidence. Methods absent here (every metadata score) rank 0, where
#: confidence still orders strong above weak. The ranking reads this tier
#: BEFORE confidence so the hierarchy never depends on float gaps.
MATCH_METHOD_RANK: Final[dict[str, int]] = {"spotify-id": 2, "isrc": 1}


def match_method_rank(method: str) -> int:
    """The categorical identity tier of a match method.

    ``0`` for any metadata evidence (or an empty/unknown method): those keep
    their confidence ordering against each other, always below an exact
    recording-id match, which is always below the platform's own id.
    """
    return MATCH_METHOD_RANK.get(method or "", 0)


@dataclass(frozen=True)
class TrackIdentity:
    """Which song — independent of where its bytes will come from.

    ``match_confidence`` is the identity's own number (how sure the match is),
    never mixed with audio quality: a 0.3-confidence FLAC is a doubtful song in
    a nice container. ISRC is best-effort (absent on most pages) and never a
    blocker — ``None`` simply means a weaker signal, not a failed lookup.
    """

    provider: str
    track_id: str
    artist: str = ""
    title: str = ""
    album: str = ""
    duration_s: Optional[int] = None
    year: Optional[int] = None
    isrc: Optional[str] = None
    match_method: str = ""
    match_confidence: float = 0.0


@dataclass(frozen=True)
class AudioCandidate:
    """One provider's offer for a track: identity plus measured audio facts.

    Codec/bitrate fields describe the *source* as the provider exposed it
    (``None`` where the provider said nothing — unknown stays unknown, never
    guessed from an extension). ``provider_verified_lossless`` is the only
    thing that can ever admit a True Lossless output: a bare ``is_lossless``
    without it is a claim, not a fact.
    """

    identity: TrackIdentity
    provider_name: str
    provider_track_id: str
    codec: Optional[str] = None
    container: Optional[str] = None
    bitrate_bps: Optional[int] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None
    is_lossless: bool = False
    provider_verified_lossless: bool = False
    match_method: str = ""
    match_confidence: float = 0.0
    #: Length the provider reported for this candidate, when it reported one.
    #: Carries the honesty gate for mapped tracks (a 500s-distant hit is a
    #: different recording, however confident the title overlap looks).
    duration_s: Optional[int] = None


@dataclass(frozen=True)
class SourceAudio:
    """What the provider served — never overwritten by what came after."""

    codec: Optional[str] = None
    container: Optional[str] = None
    bitrate_bps: Optional[int] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None
    is_lossless: bool = False
    provider_verified_lossless: bool = False


@dataclass(frozen=True)
class OutputAudio:
    """What the delivered file is — planned first, then confirmed by ffprobe.

    ``source`` is the input this output was made from (kept, not merged in):
    an upscaled transcode still names the weaker source it came from.
    ``bitrate_bps`` is the *measured* rate when a probe ran, else the encode
    target — and ``true_lossless`` follows the strict rule only (verified
    lossless source, lossless output, no lossy step anywhere).
    """

    codec: Optional[str] = None
    container: Optional[str] = None
    bitrate_bps: Optional[int] = None
    sample_rate: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None
    transcoded: bool = False
    true_lossless: bool = False
    provenance: Provenance = Provenance.UNKNOWN
    source: SourceAudio = SourceAudio()
