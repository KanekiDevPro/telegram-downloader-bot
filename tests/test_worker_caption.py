"""Regression tests for the worker's upload caption.

The old implementation used ``f"a" f"b" f"c {x}" if duration else ""`` — implicit
string concatenation binds tighter than the conditional expression, so the whole
caption collapsed to an empty string whenever the media had no duration.

The caption is also where a *quality tier* becomes honest: the buttons offer a
ceiling ("1080p (up to)"), and the caption is the only place the real resolution of
the file that arrived can be named.
"""

from __future__ import annotations

from pathlib import Path

from core.i18n import t
from services.extractor import DownloadResult, MediaInfo
from services.worker import _fmt_duration, _upload_caption

FA = "fa"


def _result(
    tmp_path: Path,
    duration: int | None,
    title: str = "A Clip",
    height: int | None = None,
) -> DownloadResult:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x" * 2048)
    info = MediaInfo(
        source_url="https://youtu.be/abc",
        title=title,
        platform="youtube",
        webpage_url="https://youtu.be/abc",
        extension="mp4",
        thumbnail=None,
        duration=duration,
        filesize_approx=2048,
        is_live=False,
        height=height,
    )
    return DownloadResult(file_path=media, info=info, media_format="video")


def test_caption_keeps_metadata_without_duration(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, duration=None), FA)
    assert "A Clip" in caption
    assert "youtube" in caption
    assert "2.0 KB" in caption
    # title + platform + size, and nothing else
    assert len(caption.splitlines()) == 3


def test_caption_includes_duration_when_known(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, duration=95), FA)
    assert "1:35" in caption
    assert len(caption.splitlines()) == 4


def test_caption_names_the_real_resolution_when_known(tmp_path: Path) -> None:
    """A 720p file answering a "1080p (up to)" tap says 720p — that is the honest
    version of the ceiling the button promised."""

    caption = _upload_caption(_result(tmp_path, duration=95, height=720), "en")

    assert "720p" in caption
    assert len(caption.splitlines()) == 5


def test_caption_invents_no_resolution_when_the_site_reports_none(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, duration=None), "en")

    assert "p" not in caption.lower().replace("a clip", "")
    assert len(caption.splitlines()) == 3


def test_caption_is_read_from_the_catalogue(tmp_path: Path) -> None:
    """Most caption lines are emoji and values (identical in any language), but they
    come from the catalogue all the same — one of them carries a word."""
    english = _upload_caption(_result(tmp_path, duration=95, height=480), "en")
    persian = _upload_caption(_result(tmp_path, duration=95, height=480), FA)

    assert "🎬 480p" in english
    assert t("work.caption_quality", FA, resolution="480p") in persian
    assert t("work.caption_duration", FA, duration="1:35") in persian


def test_caption_escapes_html_in_titles(tmp_path: Path) -> None:
    caption = _upload_caption(
        _result(tmp_path, duration=None, title="<script>alert(1)</script>"), FA
    )
    assert "&lt;script&gt;" in caption
    assert "<script>" not in caption


def test_fmt_duration() -> None:
    assert _fmt_duration(None) == ""
    assert _fmt_duration(0) == ""
    assert _fmt_duration(95) == "1:35"
    assert _fmt_duration(3725) == "1:02:05"
