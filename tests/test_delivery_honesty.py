"""The quality line under the caption: every word traceable, nothing misleading.

Three promises. Native and converted audio are labelled *distinctly* — a copied
stream is «Original» and a transcoded file names its encoding target, never the
other way round. A rate that is the encoder's target over a weaker source wears
its disclaimer (``media.upscale_mark``) wherever the label is read — caption and
cached replay alike — because a true number can still mislead about quality and
a log line is not where a user looks. And nothing is invented: no source rate,
no disclaimer, no resolution — a fact nobody reported simply has no mark.
"""

from __future__ import annotations

from services.delivery import produced_quality_label
from services.verify import upscale_disclaimer

EN = "en"
FA = "fa"


# ---------------------------------------------------------------------------
# Native vs converted — distinct labels, both languages
# ---------------------------------------------------------------------------


def test_a_copied_stream_is_original_and_claims_no_rate() -> None:
    label = produced_quality_label("audio", "m4a", ".m4a", EN)
    assert label == "M4A · Original"
    assert "kbps" not in label, "a copied stream's rate is nobody's promise"


def test_a_converted_file_names_its_target_rate_and_never_original() -> None:
    label = produced_quality_label("audio", "mp3.best", ".mp3", EN)
    assert label == "MP3 · 320 kbps"
    assert "Original" not in label, "a transcoded MP3 is never dressed as a native one"


def test_native_and_converted_labels_stay_distinct_in_both_languages() -> None:
    for lang in (EN, FA):
        native = produced_quality_label("audio", "m4a", ".m4a", lang)
        converted = produced_quality_label("audio", "mp3.best", ".mp3", lang)
        assert native != converted
        assert "320" in converted


# ---------------------------------------------------------------------------
# Upscaling is visible where the label is read — not only in a log
# ---------------------------------------------------------------------------


def test_a_target_above_the_source_carries_its_disclaimer_on_the_label() -> None:
    """320 kbps produced from a ≈130 kbps source: delivered as requested — and
    the caption says so, because «320 kbps» alone reads as a quality claim."""
    label = produced_quality_label("audio", "mp3.best", ".mp3", EN, source_kbps=130)
    assert label == "MP3 · 320 kbps · from a ≈130 kbps source"

    fa_label = produced_quality_label("audio", "mp3.best", ".mp3", FA, source_kbps=130)
    assert "130" in fa_label and "320" in fa_label, "Persian says the same fact"


def test_a_target_within_the_source_carries_no_disclaimer() -> None:
    assert produced_quality_label("audio", "mp3.small", ".mp3", EN, source_kbps=130) == (
        "MP3 · 128 kbps"
    )


def test_an_unknown_source_rate_marks_nothing_and_invents_nothing() -> None:
    assert produced_quality_label("audio", "mp3.best", ".mp3", EN) == "MP3 · 320 kbps"
    assert produced_quality_label(
        "audio", "mp3.best", ".mp3", EN, source_kbps=None
    ) == "MP3 · 320 kbps"


def test_the_disclaimer_knows_exactly_what_it_is_about() -> None:
    assert upscale_disclaimer("audio", "mp3.best", 130) == (320, 130)
    assert upscale_disclaimer("audio", "mp3.best", 320) is None, "within the source"
    assert upscale_disclaimer("audio", "mp3.best", None) is None, "unknown proves nothing"
    assert upscale_disclaimer("audio", "m4a", 130) is None, "a copied stream claims no rate"
    assert upscale_disclaimer("audio", "flac", 130) is None, "lossless has no knob"
    assert upscale_disclaimer("video", "720", 130) is None, "video heights are not rates"


def test_a_copied_stream_is_never_marked_as_an_upscale() -> None:
    """«Original» over a weak source is the site's own file — nothing to disclaim."""
    label = produced_quality_label("audio", "m4a", ".m4a", EN, source_kbps=64)
    assert label == "M4A · Original"


# ---------------------------------------------------------------------------
# Video: the label is what arrived
# ---------------------------------------------------------------------------


def test_a_video_label_never_claims_the_requested_rung() -> None:
    """A 720p request answered by a 480p file is captioned 480p (and the
    selection check refuses the delivery upstream — see tests/test_verify.py)."""
    assert produced_quality_label("video", "720", ".mp4", EN, produced_p=480) == "480p"
    assert produced_quality_label("video", "720", ".mp4", EN, produced_p=720) == "720p"
    assert produced_quality_label("video", "best", ".mp4", EN, produced_p=None) == ""
