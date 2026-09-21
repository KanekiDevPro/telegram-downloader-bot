"""Regression tests for the worker's upload caption.

The old implementation used ``f"a" f"b" f"c {x}" if duration else ""`` — implicit
string concatenation binds tighter than the conditional expression, so the whole
caption collapsed to an empty string whenever the media had no duration.
"""

from __future__ import annotations

from pathlib import Path

from services.extractor import DownloadResult, MediaInfo
from services.worker import _fmt_duration, _upload_caption


def _result(tmp_path: Path, duration: int | None, title: str = "A Clip") -> DownloadResult:
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
    )
    return DownloadResult(file_path=media, info=info, media_format="video")


def test_caption_keeps_metadata_without_duration(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, duration=None))
    assert "A Clip" in caption
    assert "youtube" in caption
    assert "2.0 KB" in caption
    # title + platform + size, and nothing else
    assert len(caption.splitlines()) == 3


def test_caption_includes_duration_when_known(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, duration=95))
    assert "1:35" in caption
    assert len(caption.splitlines()) == 4


def test_caption_escapes_html_in_titles(tmp_path: Path) -> None:
    caption = _upload_caption(_result(tmp_path, duration=None, title="<script>alert(1)</script>"))
    assert "&lt;script&gt;" in caption
    assert "<script>" not in caption


def test_fmt_duration() -> None:
    assert _fmt_duration(None) == ""
    assert _fmt_duration(0) == ""
    assert _fmt_duration(95) == "1:35"
    assert _fmt_duration(3725) == "1:02:05"
