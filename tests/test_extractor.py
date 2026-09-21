"""Offline extractor tests: URL probing, yt-dlp error mapping, and the search.

Nothing here touches the network: the search is exercised against a stand-in for
``yt_dlp.YoutubeDL`` whose answer is the *flat* entry shape yt-dlp documents for a
search (``id`` / ``url`` / ``title`` / ``duration``), which is all the Spotify
mapping needs to choose a candidate.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yt_dlp.utils import DownloadError

from services import extractor as extractor_module
from services.extractor import (
    ExtractionError,
    ExtractorService,
    SearchHit,
    _map_download_error,
)


def test_youtube_urls_are_supported() -> None:
    assert ExtractorService.is_url_supported("https://www.youtube.com/watch?v=aqz-KE-bpKQ")


def test_tiktok_urls_are_supported() -> None:
    assert ExtractorService.is_url_supported("https://www.tiktok.com/@user/video/1234567890")


def test_unknown_hosts_are_not_supported() -> None:
    assert not ExtractorService.is_url_supported(
        "https://definitely-not-real-xyz-123.example/video"
    )


def test_error_mapping_recognises_unsupported_urls() -> None:
    error = _map_download_error(DownloadError("ERROR: Unsupported URL: https://x/1"))
    assert error.code == "UNSUPPORTED_URL"


def test_error_mapping_recognises_private_videos() -> None:
    assert _map_download_error(DownloadError("ERROR: Private video")).code == "PRIVATE_VIDEO"


def test_error_mapping_recognises_timeouts() -> None:
    assert _map_download_error(DownloadError("socket timed out")).code == "TIMEOUT"


def test_error_mapping_recognises_a_stale_youtube_session() -> None:
    """YouTube's "reload the page" is a session/visitor problem, not a block."""
    error = _map_download_error(
        DownloadError("ERROR: [youtube] aqz-KE-bpKQ: The page needs to be reloaded.")
    )

    assert error.code == "SESSION_STALE"
    assert "کوکی تازه" in error.message


def test_a_drm_site_reaches_the_extractor_and_is_refused_there() -> None:
    """The offline probe says "supported" for a DRM site (yt-dlp's own DRM extractor
    matches the URL), which is why the refusal has to be classified *here* — nothing
    upstream would have caught the link. (Spotify is the one exception, and only
    because the gateway rewrites it before yt-dlp sees it — see services/spotify.py.)"""
    assert ExtractorService.is_url_supported("https://www.deezer.com/track/123456")
    assert ExtractorService.is_url_supported(
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
    )


def test_error_mapping_recognises_a_drm_site() -> None:
    """The exact sentence yt-dlp answers ``KnownDRMIE`` hosts (Spotify, Disney+, …)
    with — measured from a real run, not paraphrased."""
    error = _map_download_error(
        DownloadError(
            "ERROR: [DRM] The requested site is known to use DRM protection. "
            "It will NOT be supported.\n       Please DO NOT open an issue"
        )
    )

    assert error.code == "DRM_PROTECTED"
    assert "DRM" in error.message
    # Not the block message: a policy refusal is not "the site refused us", and
    # telling the user to try again later would be wrong.
    blocked = _map_download_error(DownloadError("ERROR: Sign in to confirm you're not a bot"))
    assert blocked.code == "EXTRACTOR_BLOCKED"
    assert error.message != blocked.message


class _FakeYDL:
    """Records the options it was built with and answers one canned search."""

    instances: list["_FakeYDL"] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.calls: list[str] = []
        _FakeYDL.instances.append(self)

    def __enter__(self) -> "_FakeYDL":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        self.calls.append(url)
        return {
            "_type": "playlist",
            "entries": [
                {
                    "_type": "url",
                    "ie_key": "Youtube",
                    "id": "dQw4w9WgXcQ",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "title": "Official",
                    "duration": 213,
                },
                # A flat entry whose watch URL is missing still has its id — that is
                # enough to build the URL it stands for.
                {"_type": "url", "id": "abcdefghijk", "url": None, "title": "Id only"},
                "a shape nobody expects",
            ],
        }


def _search(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, limit: int = 5) -> list[SearchHit]:
    service = ExtractorService(tmp_path, cookie_file=None, js_runtime="none")
    monkeypatch.setattr(extractor_module, "yt_dlp", SimpleNamespace(YoutubeDL=_FakeYDL))
    return service._search_sync("Rick Astley Never Gonna Give You Up", limit)


def test_a_search_asks_youtube_for_flat_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Flat on purpose: extracting every candidate would cost the user a dozen
    extractions for the one track they asked about."""
    hits = _search(monkeypatch, tmp_path)

    ydl = _FakeYDL.instances[-1]
    assert ydl.calls == ["ytsearch5:Rick Astley Never Gonna Give You Up"]
    assert ydl.opts["extract_flat"] == "in_playlist"
    assert ydl.opts["playlistend"] == 5
    assert ydl.opts["skip_download"] is True
    assert hits == [
        SearchHit("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Official", 213),
        SearchHit("https://www.youtube.com/watch?v=abcdefghijk", "Id only", None),
    ]


def test_the_search_limit_is_the_one_that_was_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _search(monkeypatch, tmp_path, limit=3)

    assert _FakeYDL.instances[-1].calls == ["ytsearch3:Rick Astley Never Gonna Give You Up"]


def test_a_search_that_crashes_is_translated_like_any_other_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocked search must arrive as a *block*, not as a yt-dlp exception."""
    service = ExtractorService(tmp_path, cookie_file=None, js_runtime="none")

    class Blocked(_FakeYDL):
        def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
            raise DownloadError("ERROR: Sign in to confirm you're not a bot")

    monkeypatch.setattr(extractor_module, "yt_dlp", SimpleNamespace(YoutubeDL=Blocked))

    with pytest.raises(ExtractionError) as caught:
        service._search_sync("anything", 5)

    assert caught.value.code == "EXTRACTOR_BLOCKED"


@pytest.mark.parametrize(
    "text",
    [
        # The exact sentences yt-dlp answers a post with no *video* in it with — an
        # image post, or a text-only one. Measured from its own sources: twitter,
        # reddit, and the bluesky/tumblr pair (which share one message).
        "ERROR: [twitter] 12345: No video could be found in this tweet",
        "ERROR: [reddit] abc: No media found",
        "ERROR: [bluesky] xyz: No video could be found in this post",
    ],
)
def test_error_mapping_recognises_a_post_with_no_video(text: str) -> None:
    """Not a failure of ours and not a dead end: it is the other engine's job, and
    that engine is exactly why this code is classified rather than generalised."""
    error = _map_download_error(DownloadError(text))

    assert error.code == "IMAGE_ONLY"
    assert error.code != "GENERAL"
    assert "ویدیو" in error.message


def test_error_mapping_falls_back_to_general() -> None:
    error = _map_download_error(DownloadError("something nobody predicted"))
    assert error.code == "GENERAL"
    assert "something nobody predicted" in error.message
