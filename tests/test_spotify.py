"""Spotify links: what is read from them, and which video stands in for one.

Two measured facts are pinned here, because the whole module only makes sense with
them: yt-dlp answers these links from its ``KnownDRMIE`` list, and the embedded
Cobalt has no Spotify service — so neither engine can fetch the link, and the only
route that can ever work is the *same song somewhere else*. What this file checks is
that the somewhere-else is chosen honestly: the metadata comes from Spotify's own
public page (the payload below is the real shape, not a paraphrase), and a candidate
that is plainly a different recording is refused instead of downloaded.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from services import spotify
from services.extractor import ExtractionError, SearchHit, url_host

TRACK_ID = "4uLU6hMCjMI75M1A2tKUQC"
TRACK_URL = f"https://open.spotify.com/track/{TRACK_ID}"

#: The locale-specific image host a real embed page answers with.
LOCALE_CDN = "https://image-cdn-fa.spotifycdn.com/image"

#: The real ``__NEXT_DATA__`` payload of a track's embed page, trimmed to the keys
#: this module reads plus a few it ignores (measured live, not invented).
EMBED_ENTITY = {
    "artists": [
        {"name": "Rick Astley", "uri": "spotify:artist:0gxyHStUsqpMadRV0Di1Qt"},
    ],
    "duration": 213573,
    "id": TRACK_ID,
    "isPlayable": True,
    "name": "Never Gonna Give You Up",
    "title": "Never Gonna Give You Up",
    "type": "track",
    "uri": f"spotify:track:{TRACK_ID}",
    "audioPreview": {"url": "https://p.scdn.co/mp3-preview/abc"},
    "releaseDate": {"isoString": "1987-11-12T00:00:00Z"},
    # The embed's image URLs are locale-specific (``image-cdn-fa``); the id is what
    # is stable, and it is what the canonical host is rebuilt from.
    "visualIdentity": {
        "image": [
            {"url": f"{LOCALE_CDN}/ab67616d00001e02abc", "maxHeight": 300, "maxWidth": 300},
            {"url": f"{LOCALE_CDN}/ab67616d00004851abc", "maxHeight": 64, "maxWidth": 64},
            {"url": f"{LOCALE_CDN}/ab67616d0000b273abc", "maxHeight": 640, "maxWidth": 640},
        ]
    },
}

#: The track page, as Spotify writes it: the album is in this line and nowhere else.
SEARCH_PAGE_HTML = (
    "<html><head>"
    '<meta property="og:title" content="Never Gonna Give You Up"/>'
    '<meta property="og:description" content="Rick Astley · Whenever You Need '
    'Somebody · Song · 1987"/>'
    '<meta property="music:album" content="https://open.spotify.com/album/6N9PS4QXF1D0OWPk0Sxtb4"/>'
    "</head><body></body></html>"
)


def _embed_html(entity: dict[str, object] | None = None) -> str:
    payload = {"props": {"pageProps": {"state": {"data": {"entity": entity or EMBED_ENTITY}}}}}
    return (
        "<!DOCTYPE html><html><head>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</head><body></body></html>"
    )


TRACK = spotify.SpotifyTrack(
    track_id=TRACK_ID, title="Never Gonna Give You Up", artists=("Rick Astley",), duration_s=213
)


def _hit(title: str, duration_s: int | None, video_id: str = "dQw4w9WgXcQ") -> SearchHit:
    return SearchHit(url=f"https://www.youtube.com/watch?v={video_id}", title=title, duration_s=duration_s)


def test_the_metadata_that_makes_it_a_spotify_track_is_read_too() -> None:
    """Title, artist, year and artwork are what the delivered file is tagged with —
    and the cover is rewritten onto the canonical host, because the embed's own URL
    only resolves on Spotify's localized CDN."""
    track = spotify._read_track(_embed_html(), TRACK_ID)

    assert track.year == 1987
    assert dict(track.covers) == {
        64: "https://i.scdn.co/image/ab67616d00004851abc",
        300: "https://i.scdn.co/image/ab67616d00001e02abc",
        640: "https://i.scdn.co/image/ab67616d0000b273abc",
    }
    assert track.artist == "Rick Astley"


def test_the_thumbnail_is_the_size_telegram_accepts() -> None:
    """Telegram refuses a thumbnail wider than 320px, so the 640px cover Spotify
    publishes is not one — the 300px size is, and it is sent. When every cover is
    oversized the file travels tagged but without artwork, never with a bad one."""
    track = spotify._read_track(_embed_html(), TRACK_ID)
    huge = spotify.SpotifyTrack(TRACK_ID, "Song", ("Artist",), 1, covers=((640, "https://x/640"),))

    assert track.thumbnail_url == "https://i.scdn.co/image/ab67616d00001e02abc"
    assert huge.thumbnail_url == ""
    assert spotify.SpotifyTrack(TRACK_ID, "Song", (), None).thumbnail_url == ""


@pytest.mark.parametrize(
    ("html", "album"),
    (
        (SEARCH_PAGE_HTML, "Whenever You Need Somebody"),
        # The label before the year is localised, so the *shape* is what is checked:
        # a Persian page (whose "Song" is «آهنگ») still yields the album, and a page
        # of an unknown shape yields nothing rather than a wrong guess.
        ('<meta property="og:description" content="Artist · Album · آهنگ · 1400"/>', "Album"),
        ('<meta property="og:description" content="Artist · Album · Song"/>', ""),
        ('<meta property="og:description" content="Artist · Album · Song · 1987 · x"/>', ""),
        ("<html>nothing here</html>", ""),
        (
            '<meta property="og:description" content="Rick &amp; Co · A &amp; B · Song · 1999"/>',
            "A & B",
        ),
    ),
)
def test_the_album_is_read_from_the_one_place_spotify_publishes_it(html: str, album: str) -> None:
    assert spotify.album_from_page(html) == album


async def test_a_missing_album_costs_a_line_of_caption_not_the_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The album lives on a second page; when that page is gone the track is still
    complete enough to download and tag."""
    async def canonical(session: object, url: str, timeout: float) -> str:
        return TRACK_URL

    async def get(session: object, url: str, timeout: float) -> str:
        if "/track/" in url and "/embed/" not in url:
            raise ExtractionError("SPOTIFY_LOOKUP_FAILED", "unavailable")
        return _embed_html()

    monkeypatch.setattr(spotify, "_canonical", canonical)
    monkeypatch.setattr(spotify, "_get", get)

    track = await spotify.lookup(TRACK_URL)

    assert track.title == "Never Gonna Give You Up"
    assert track.album == ""


async def test_a_track_is_read_with_its_album_when_the_page_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def canonical(session: object, url: str, timeout: float) -> str:
        return TRACK_URL

    async def get(session: object, url: str, timeout: float) -> str:
        return SEARCH_PAGE_HTML if "/embed/" not in url else _embed_html()

    monkeypatch.setattr(spotify, "_canonical", canonical)
    monkeypatch.setattr(spotify, "_get", get)

    track = await spotify.lookup(TRACK_URL)

    assert track.album == "Whenever You Need Somebody"
    assert track.year == 1987
    assert track.thumbnail_url


class FakeExtractor:
    """Only ``search`` is ever called — and it records what it was asked for."""

    def __init__(self, hits: list[SearchHit] | None = None, error: Exception | None = None) -> None:
        self.hits = hits if hits is not None else []
        self.error = error
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.hits


# ---------------------------------------------------------------------------
# What counts as a Spotify link, and which part of it is a track
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        TRACK_URL,
        f"https://open.spotify.com/intl-de/track/{TRACK_ID}?si=abc123",
        f"https://spotify.com/track/{TRACK_ID}",
        "https://spotify.link/abcDEF123",
    ],
)
def test_spotify_links_are_recognised(url: str) -> None:
    assert spotify.is_spotify_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "https://open.spotify.example.com/track/x",  # look-alike host, not a subdomain
        "https://soundcloud.com/artist/track",
        "not a url",
    ],
)
def test_other_links_are_not(url: str) -> None:
    assert not spotify.is_spotify_url(url)
    assert url_host(url)  # ``url_host`` never raises, whatever it is handed


def test_the_track_id_survives_locale_prefixes_and_share_parameters() -> None:
    assert spotify.track_id(TRACK_URL) == TRACK_ID
    assert spotify.track_id(f"https://open.spotify.com/intl-de/track/{TRACK_ID}?si=x") == TRACK_ID


def test_an_album_is_not_a_track_to_map() -> None:
    """An album is a list of songs: downloading the first one answers another question."""
    assert spotify.track_id("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3") is None
    assert spotify.track_id("https://spotify.link/abcDEF123") is None


# ---------------------------------------------------------------------------
# Reading the track
# ---------------------------------------------------------------------------


def test_a_track_is_read_from_the_public_embed_page() -> None:
    track = spotify._read_track(_embed_html(), TRACK_ID)

    assert track.track_id == TRACK_ID
    assert track.title == "Never Gonna Give You Up"
    assert track.artists == ("Rick Astley",)
    assert track.duration_s == 213  # milliseconds in, seconds out


def test_the_credit_and_the_search_query_read_like_a_human_wrote_them() -> None:
    assert TRACK.credit == "Rick Astley — Never Gonna Give You Up"
    # Artists first: the title alone ranks covers and karaoke just as highly.
    assert TRACK.search_query == "Rick Astley Never Gonna Give You Up"
    # A track with no artist metadata still has a title to search for.
    assert spotify.SpotifyTrack("1", "Song", (), None).search_query == "Song"
    assert spotify.SpotifyTrack("1", "Song", (), None).credit == "Song"


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>no next data here</body></html>",
        '<script id="__NEXT_DATA__" type="application/json">{not json}</script>',
        '<script id="__NEXT_DATA__" type="application/json">{"props": {}}</script>',
    ],
)
def test_a_page_that_changed_shape_says_so_instead_of_guessing(html: str) -> None:
    with pytest.raises(ExtractionError) as caught:
        spotify._read_track(html, TRACK_ID)

    assert caught.value.code == "SPOTIFY_LOOKUP_FAILED"
    assert caught.value.message  # never a bare code: the user reads this


async def test_lookup_follows_a_share_link_to_the_embed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """``spotify.link`` is the app's share domain; the track id is only behind it."""
    seen: list[str] = []

    async def canonical(session: object, url: str, timeout: float) -> str:
        assert url == "https://spotify.link/abcDEF123"
        return TRACK_URL

    async def get(session: object, url: str, timeout: float) -> str:
        seen.append(url)
        return _embed_html()

    monkeypatch.setattr(spotify, "_canonical", canonical)
    monkeypatch.setattr(spotify, "_get", get)

    track = await spotify.lookup("https://spotify.link/abcDEF123")

    assert track.title == "Never Gonna Give You Up"
    # The embed page first (that is the track), then the track page (that is the
    # album) — both derived from the same base URL.
    assert seen == [
        f"https://open.spotify.com/embed/track/{TRACK_ID}",
        f"https://open.spotify.com/track/{TRACK_ID}",
    ]


async def test_lookup_refuses_a_link_that_is_not_a_track(monkeypatch: pytest.MonkeyPatch) -> None:
    async def canonical(session: object, url: str, timeout: float) -> str:
        return "https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3"

    async def get(session: object, url: str, timeout: float) -> str:
        raise AssertionError("an album should never reach the embed fetch")

    monkeypatch.setattr(spotify, "_canonical", canonical)
    monkeypatch.setattr(spotify, "_get", get)

    with pytest.raises(ExtractionError) as caught:
        await spotify.lookup("https://open.spotify.com/album/1DFixLWuPkv3KT3TnV35m3")

    assert caught.value.code == "SPOTIFY_NOT_A_TRACK"


# ---------------------------------------------------------------------------
# Choosing the stand-in
# ---------------------------------------------------------------------------


def test_the_closest_length_wins_over_the_search_ranking() -> None:
    """A live version at the top must lose to the studio track further down."""
    hits = [_hit("Rick Astley - Never Gonna Give You Up (Live)", 350), _hit("Official", 215)]

    assert spotify.best_hit(TRACK, hits) is hits[1]


def test_a_candidate_that_is_plainly_another_recording_is_refused() -> None:
    """A ten-hour loop matches the relevance ranking and nothing else."""
    hits = [_hit("Never Gonna Give You Up (10 hours)", 36000, "long")]

    assert spotify.best_hit(TRACK, hits) is None


def test_without_lengths_the_ranking_is_trusted() -> None:
    hits = [_hit("First", None, "one"), _hit("Second", 213, "two")]

    # One candidate carries a length → that one is the better answer.
    assert spotify.best_hit(TRACK, hits) is hits[1]
    # Nobody does → the first hit is the best available answer.
    assert spotify.best_hit(TRACK, [_hit("First", None, "one"), _hit("Second", None, "two")]) == hits[0]


def test_without_a_spotify_length_the_first_hit_is_taken() -> None:
    unknown = spotify.SpotifyTrack(TRACK_ID, "Song", ("Artist",), None)
    hits = [_hit("First", 999, "one"), _hit("Second", 42, "two")]

    assert spotify.best_hit(unknown, hits) is hits[0]


def test_no_candidates_at_all_is_not_a_match() -> None:
    assert spotify.best_hit(TRACK, []) is None


# ---------------------------------------------------------------------------
# The whole mapping
# ---------------------------------------------------------------------------


async def test_a_track_becomes_the_youtube_video_that_should_stand_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(url: str, *, timeout: float = 0) -> spotify.SpotifyTrack:
        return TRACK

    monkeypatch.setattr(spotify, "lookup", lookup)
    extractor = FakeExtractor([_hit("Rick Astley - Never Gonna Give You Up", 213)])

    target = await spotify.youtube_target(TRACK_URL, extractor)  # type: ignore[arg-type]

    assert target.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert target.track is TRACK
    assert extractor.queries == ["Rick Astley Never Gonna Give You Up"]


async def test_a_search_that_finds_nothing_is_said_in_the_user_s_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lookup(url: str, *, timeout: float = 0) -> spotify.SpotifyTrack:
        return TRACK

    monkeypatch.setattr(spotify, "lookup", lookup)

    with pytest.raises(ExtractionError) as caught:
        await spotify.youtube_target(TRACK_URL, FakeExtractor([]))  # type: ignore[arg-type]

    assert caught.value.code == "SPOTIFY_NO_MATCH"
    assert "اسپاتیفای" in caught.value.message


async def test_the_gateway_lets_a_spotify_link_through_without_asking_yt_dlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway's probe is yt-dlp's opinion, and its opinion of Spotify is "known
    to use DRM protection". The link is served by rewriting it, so that opinion must
    not decide whether the user may queue it."""
    from handlers import user as user_module
    from services.extractor import ExtractorService

    def explode(url: str) -> bool:
        raise AssertionError("yt-dlp must not be consulted for a Spotify link")

    monkeypatch.setattr(ExtractorService, "is_url_supported", staticmethod(explode))

    assert await user_module._probe_supported(TRACK_URL)


# ---------------------------------------------------------------------------
# Over real HTTP (the client, the redirect and the parse — not a stubbed session)
# ---------------------------------------------------------------------------


class _SpotifyOrigin(BaseHTTPRequestHandler):
    """The pages a lookup can land on: a share hop, the embed, the track page — and
    the image CDN the cover is fetched from."""

    #: A minimal thing that is announced as image/jpeg (its bytes are not parsed).
    COVER = b"\xff\xd8\xff" + b"x" * 512

    def do_GET(self) -> None:  # noqa: N802 — http.server's own API
        if self.path.startswith("/share/"):
            self.send_response(302)
            self.send_header("Location", f"/track/{TRACK_ID}")
            self.end_headers()
            return
        if self.path.startswith(f"/embed/track/{TRACK_ID}"):
            self._body(_embed_html())
            return
        if self.path.startswith(f"/track/{TRACK_ID}"):
            self._body(SEARCH_PAGE_HTML)
            return
        if self.path.startswith("/image/"):
            self._body(
                self.COVER, content_type="image/jpeg"
            )
            return
        self.send_response(404)
        self.end_headers()

    def _body(self, html: str | bytes, content_type: str = "text/html; charset=utf-8") -> None:
        body = html.encode("utf-8") if isinstance(html, str) else html
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Silence it: this suite's output is its assertions."""


@pytest.fixture()
def origin(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real HTTP origin, with Spotify's host patched to point at it.

    Only the *host* is local: the request, the redirect, the status handling and
    the parse all run for real, and nothing here reaches the internet.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SpotifyOrigin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host = str(server.server_address[0])  # typeshed types this as `str | bytes`
    port = int(server.server_address[1])
    monkeypatch.setattr(
        spotify, "_EMBED_URL", f"http://{host}:{port}/embed/track/{{track_id}}"
    )
    # The image CDN is a host too: pointing it at the origin keeps the cover fetch
    # a real HTTP request instead of a bare function call.
    monkeypatch.setattr(spotify, "_IMAGE_CDN", f"http://{host}:{port}/image/{{image_id}}")
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


async def test_a_track_is_read_over_real_http(origin: str) -> None:
    track = await spotify.lookup(TRACK_URL)

    assert track.credit == "Rick Astley — Never Gonna Give You Up"
    assert track.duration_s == 213
    assert track.album == "Whenever You Need Somebody"


async def test_the_cover_reaches_the_disk_over_real_http(
    origin: str, tmp_path: Path
) -> None:
    """The artwork is fetched, size-checked and written next to the job's files."""
    track = await spotify.lookup(TRACK_URL)

    path = await spotify.download_cover(track, tmp_path)

    assert path is not None and path.is_file()
    assert path.read_bytes().startswith(b"\xff\xd8\xff")
    assert path.parent == tmp_path


async def test_a_cover_telegram_would_refuse_is_not_written(
    origin: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram's 200 kB thumbnail ceiling is checked *before* the upload, so an
    oversized image costs the artwork rather than the whole file."""
    monkeypatch.setattr(spotify, "MAX_COVER_BYTES", 10)
    track = await spotify.lookup(TRACK_URL)

    assert await spotify.download_cover(track, tmp_path) is None
    assert not (tmp_path / spotify.COVER_FILE_NAME).exists()


async def test_a_track_without_artwork_is_not_a_request(tmp_path: Path) -> None:
    bare = spotify.SpotifyTrack(TRACK_ID, "Song", ("Artist",), 120)

    assert await spotify.download_cover(bare, tmp_path) is None


async def test_a_share_link_is_followed_over_real_http(origin: str) -> None:
    """``spotify.link`` answers with a redirect, and the track id is behind it."""
    track = await spotify.lookup(f"{origin}/share/abcDEF123")

    assert track.track_id == TRACK_ID


async def test_a_status_that_is_not_200_is_reported_plainly(origin: str) -> None:
    with pytest.raises(ExtractionError) as caught:
        await spotify.lookup("https://open.spotify.com/track/0000000000000000000000")

    assert caught.value.code == "SPOTIFY_LOOKUP_FAILED"
    assert "404" in caught.value.message


async def test_a_blocked_search_keeps_the_block_diagnosis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YouTube refusing the *search* is a block, and the block machinery owns that
    answer — inventing a Spotify-specific one here would hide the real cause."""
    async def lookup(url: str, *, timeout: float = 0) -> spotify.SpotifyTrack:
        return TRACK

    monkeypatch.setattr(spotify, "lookup", lookup)
    blocked = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    extractor = FakeExtractor(error=blocked)

    with pytest.raises(ExtractionError) as caught:
        await spotify.youtube_target(TRACK_URL, extractor)  # type: ignore[arg-type]

    assert caught.value is blocked
