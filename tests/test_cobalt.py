"""The Cobalt client: the request it makes, and every answer it can receive.

This is the only network-facing piece of the fallback path, and it is the piece an
operator cannot debug from a user's message — so the contract is pinned here: the
documented payload, the newer schema as a retry rather than a guess, all three
success statuses, the album answer, the instance's own error codes, and the
transport failures, none of which may escape as anything but a CobaltError.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Optional

import aiohttp
import pytest

from services.cobalt import (
    CobaltError,
    CobaltMedia,
    CobaltPart,
    CobaltService,
    url_is_fetchable,
)

URL = "https://api.cobalt.example"


# ---------------------------------------------------------------------------
# A stand-in for aiohttp.ClientSession
# ---------------------------------------------------------------------------


class FakeContent:
    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = list(chunks)

    def iter_chunked(self, size: int) -> Any:
        async def generate() -> Any:
            for chunk in self._chunks:
                yield chunk

        return generate()


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        body: Any = None,
        headers: Optional[dict[str, str]] = None,
        chunks: Iterable[bytes] = (),
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)
        self._body = body

    async def json(self, **kwargs: Any) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class FakeSession:
    """Records requests; answers with the queued responses in order."""

    def __init__(
        self,
        *,
        post_responses: Iterable[FakeResponse] = (),
        get_response: FakeResponse | None = None,
        get_responses: Iterable[FakeResponse] = (),
        error: Exception | None = None,
    ) -> None:
        self._post_responses = list(post_responses)
        self._get_response = get_response
        #: A gallery is several GETs; a single download keeps using ``get_response``.
        self._get_responses = list(get_responses)
        self._error = error
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append({"url": url, **kwargs})
        if self._error is not None:
            raise self._error
        return self._post_responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        if self._error is not None:
            raise self._error
        if self._get_responses:
            return self._get_responses.pop(0)
        assert self._get_response is not None, "no download response queued"
        return self._get_response

    async def close(self) -> None:
        self.closed = True


def _service(session: FakeSession, **kwargs: Any) -> CobaltService:
    return CobaltService(URL, session=session, **kwargs)  # type: ignore[arg-type]


def _stream(url: str = "https://cdn.example/v.mp4", **extra: Any) -> FakeResponse:
    body = {"status": "stream", "url": url, **extra}
    return FakeResponse(body=body)


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


async def test_the_request_is_the_documented_one() -> None:
    session = FakeSession(post_responses=[_stream()])

    media = await _service(session).resolve("https://youtu.be/abc", "video")

    assert len(session.posts) == 1
    request = session.posts[0]
    assert request["url"] == f"{URL}/api/json"
    assert request["json"] == {
        "url": "https://youtu.be/abc",
        # ``max`` is Cobalt's own word for the no-ceiling request yt-dlp gets as
        # ``bestvideo``: the two engines must not disagree about what was asked for.
        "vQuality": "max",
        "filenamePattern": "nerd",
    }
    assert request["headers"]["Accept"] == "application/json"
    assert request["headers"]["Content-Type"] == "application/json"
    assert "Authorization" not in request["headers"]
    assert media == CobaltMedia(url="https://cdn.example/v.mp4")


async def test_an_audio_request_asks_for_mp3_server_side() -> None:
    """The fallback must not need ffmpeg — the primary path may have just lost it."""
    session = FakeSession(post_responses=[_stream()])

    await _service(session).resolve("https://youtu.be/abc", "audio")

    assert session.posts[0]["json"]["isAudioOnly"] is True
    assert session.posts[0]["json"]["aFormat"] == "mp3"


async def test_a_quality_tier_reaches_the_instance_unchanged() -> None:
    """The tier is the user's choice, not the fallback's: a 480p ask that lands on
    the other engine must still come back as 480p."""
    session = FakeSession(post_responses=[_stream(), _stream()])

    service = _service(session)
    await service.resolve("https://youtu.be/abc", "video", "480")
    await service.resolve("https://youtu.be/abc", "audio", "m4a")

    assert session.posts[0]["json"]["vQuality"] == "480"
    assert session.posts[1]["json"]["aFormat"] == "best", "M4A means the untouched stream"


async def test_an_api_key_is_sent_only_when_there_is_one() -> None:
    session = FakeSession(post_responses=[_stream()])

    await _service(session, api_key="secret").resolve("https://youtu.be/abc", "video")

    assert session.posts[0]["headers"]["Authorization"] == "Api-Key secret"


async def test_a_schema_rejection_is_replayed_in_the_newer_schema() -> None:
    """Which schema an instance accepts is unknowable; a 400 that names a field is."""
    session = FakeSession(
        post_responses=[
            FakeResponse(status=400, body={"status": "error", "text": "unknown field: vQuality"}),
            _stream(),
        ]
    )

    media = await _service(session).resolve("https://youtu.be/abc", "video")

    assert media.url == "https://cdn.example/v.mp4"
    assert len(session.posts) == 2
    first, second = session.posts[0], session.posts[1]
    # The path moved too, not just the field names: v10 serves the API at the root.
    assert first["url"] == f"{URL}/api/json"
    assert second["url"] == URL
    assert first["json"]["vQuality"] == "max"
    assert second["json"]["videoQuality"] == "max"
    # ``pretty``, not ``nerd``: the live v10 instance rejects the latter outright
    # (`error.api.invalid_body`), and it is the same API the stack now embeds.
    assert second["json"]["filenameStyle"] == "pretty"
    assert second["json"]["downloadMode"] == "auto"
    assert "vQuality" not in second["json"]


async def test_the_retired_v7_endpoint_falls_through_to_the_current_api() -> None:
    """The message the public instance answers /api/json with since Nov 2024.

    A *working* v10+ instance is on the other side of it, so it must never reach the
    user as a failure.
    """
    session = FakeSession(
        post_responses=[
            FakeResponse(
                status=400,
                body={
                    "status": "error",
                    "text": "the cobalt v7 api has been shut down on nov 11th 2024. see ...",
                },
            ),
            _stream(),
        ]
    )

    media = await _service(session).resolve("https://youtu.be/abc", "video")

    assert media.url == "https://cdn.example/v.mp4"
    assert [post["url"] for post in session.posts] == [f"{URL}/api/json", URL]


async def test_the_working_shape_is_remembered_for_the_next_link() -> None:
    """One wasted round trip per process, not one per blocked link."""
    session = FakeSession(
        post_responses=[
            FakeResponse(status=400, body={"text": "the v7 api has been shut down"}),
            _stream("https://cdn.example/1.mp4"),
            _stream("https://cdn.example/2.mp4"),
        ]
    )
    service = _service(session)

    await service.resolve("https://youtu.be/one", "video")
    await service.resolve("https://youtu.be/two", "video")

    assert [post["url"] for post in session.posts] == [f"{URL}/api/json", URL, URL]


async def test_the_instance_s_own_code_is_kept_for_the_caller() -> None:
    """A YouTube-only gap is a different fix from a broken instance, and the doctor
    has to tell them apart without matching Persian text."""
    session = FakeSession(
        post_responses=[
            FakeResponse(status=404, body={}),  # the retired v7 path, as v10 answers it
            FakeResponse(
                status=200,
                body={"status": "error", "error": {"code": "error.api.youtube.login"}},
            ),
        ]
    )
    service = _service(session)

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/abc", "video")

    assert len(session.posts) == 2, "one dialect probe, then the real answer"
    assert caught.value.upstream == "error.api.youtube.login"
    assert caught.value.youtube_session_missing
    assert not caught.value.instance, "one service must not silence the instance"
    assert not service.quarantined, "and it stays available for tiktok/instagram"


async def test_the_dialect_is_known_even_when_the_answer_is_a_refusal() -> None:
    """The public v10 instance refuses anonymous callers — but it refuses *in the
    v10 shape*, and that is what the report has to be able to print: "your
    instance speaks v10 and wants a key" is a different fix from "we could not
    reach it at all"."""
    session = FakeSession(
        post_responses=[
            FakeResponse(status=400, body={"text": "the cobalt v7 api has been shut down"}),
            FakeResponse(
                status=400,
                body={"status": "error", "error": {"code": "error.api.auth.jwt.missing"}},
            ),
        ]
    )
    service = _service(session)

    with pytest.raises(CobaltError):
        await service.resolve("https://youtu.be/abc", "video")

    assert service.dialect == "v10"


async def test_an_unrelated_400_is_not_retried() -> None:
    """A bad link is a bad link; replaying it in another schema would be noise."""
    session = FakeSession(
        post_responses=[
            FakeResponse(status=400, body={"status": "error", "error": {"code": "error.api.link.invalid"}}),
        ]
    )

    with pytest.raises(CobaltError) as caught:
        await _service(session).resolve("https://example.com/nope", "video")

    assert caught.value.code == "ERROR"
    assert len(session.posts) == 1


async def test_an_instance_that_fails_as_an_instance_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyless public instance would otherwise cost one round trip per blocked
    link, and a log line per blocked link, for as long as the deployment runs."""
    session = FakeSession(
        post_responses=[
            FakeResponse(status=400, body={"text": "the v7 api has been shut down"}),
            FakeResponse(
                status=400,
                body={"status": "error", "error": {"code": "error.api.auth.jwt.missing"}},
            ),
        ]
    )
    service = _service(session)
    assert service.available

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/abc", "video")

    assert caught.value.instance is True
    assert "احراز هویت" in caught.value.message
    assert not service.available and service.quarantined
    # The next blocked link does not even try — the caller sees `available` and
    # keeps the original yt-dlp diagnosis.
    with pytest.raises(CobaltError) as again:
        await service.resolve("https://youtu.be/def", "video")
    assert again.value.instance is True
    assert len(session.posts) == 2


async def test_the_quarantine_expires() -> None:
    session = FakeSession(
        post_responses=[FakeResponse(status=503, body={"text": "maintenance"}), _stream()]
    )
    service = _service(session)

    with pytest.raises(CobaltError):
        await service.resolve("https://youtu.be/abc", "video")
    assert not service.available

    service._quarantine_until = 0.0  # the window passes
    media = await service.resolve("https://youtu.be/abc", "video")

    assert media.url == "https://cdn.example/v.mp4"
    assert service.available


async def test_a_link_problem_does_not_silence_a_working_instance() -> None:
    """A private video is not a reason to stop serving the next link."""
    session = FakeSession(
        post_responses=[
            FakeResponse(
                body={
                    "status": "error",
                    "error": {"code": "error.api.content.video.unavailable"},
                }
            ),
            _stream(),
        ]
    )
    service = _service(session)

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/private", "video")

    assert caught.value.instance is False
    assert service.available
    assert (await service.resolve("https://youtu.be/other", "video")).url.endswith("v.mp4")


# ---------------------------------------------------------------------------
# The answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["stream", "redirect", "tunnel"])
async def test_every_success_status_carries_the_link(status: str) -> None:
    session = FakeSession(post_responses=[FakeResponse(body={"status": status, "url": "https://x/1"})])

    assert (await _service(session).resolve("https://youtu.be/abc", "video")).url == "https://x/1"


async def test_an_empty_picker_is_not_a_download() -> None:
    """A *non-empty* picker is a multi-media post and is served (it is how an image
    post with several photos arrives); an empty one is a link with nothing in it."""
    session = FakeSession(post_responses=[FakeResponse(body={"status": "picker", "picker": []})])

    with pytest.raises(CobaltError) as caught:
        await _service(session).resolve("https://youtu.be/playlist", "video")

    assert caught.value.code == "NO_MEDIA"


async def test_the_instance_s_own_error_code_reaches_the_log() -> None:
    session = FakeSession(
        post_responses=[
            FakeResponse(
                body={"status": "error", "error": {"code": "error.api.rate_exceeded"}},
            )
        ]
    )

    with pytest.raises(CobaltError) as caught:
        await _service(session).resolve("https://youtu.be/abc", "video")

    assert caught.value.code == "ERROR"
    assert "محدودیت نرخ" in caught.value.message
    assert "error.api.rate_exceeded" in caught.value.message


async def test_a_refusal_names_the_likely_reason() -> None:
    for status in (401, 403):
        session = FakeSession(post_responses=[FakeResponse(status=status, body={})])

        with pytest.raises(CobaltError) as caught:
            await _service(session).resolve("https://youtu.be/abc", "video")

        assert caught.value.code == "REFUSED"
        assert "کلید" in caught.value.message


async def test_a_rate_limited_instance_says_so() -> None:
    session = FakeSession(post_responses=[FakeResponse(status=429, body={})])

    with pytest.raises(CobaltError) as caught:
        await _service(session).resolve("https://youtu.be/abc", "video")

    assert caught.value.code == "REFUSED"
    assert "429" in caught.value.message


async def test_a_body_without_a_link_is_a_protocol_mismatch() -> None:
    session = FakeSession(post_responses=[FakeResponse(body={"status": "local-processing"})])

    with pytest.raises(CobaltError) as caught:
        await _service(session).resolve("https://youtu.be/abc", "video")

    assert caught.value.code == "BAD_RESPONSE"
    assert "local-processing" in caught.value.message


# ---------------------------------------------------------------------------
# Links the services write badly (the repair, not the outage)
# ---------------------------------------------------------------------------


async def test_a_doubled_scheme_in_the_link_is_repaired() -> None:
    """streamable's API answers `https:https://cdn…` — straight from the source.

    Cobalt passes a service's link through untouched, so without this the fallback
    would fail the *download* on a URL that is one character away from working —
    and the failure would look like an instance outage.
    """
    session = FakeSession(
        post_responses=[
            FakeResponse(body={"status": "redirect", "url": "https:https://cdn-cf.example/v.mp4"})
        ]
    )

    media = await _service(session).resolve("https://streamable.com/moo", "video")

    assert media.url == "https://cdn-cf.example/v.mp4"


async def test_a_protocol_relative_link_borrows_https() -> None:
    session = FakeSession(
        post_responses=[FakeResponse(body={"status": "redirect", "url": "//cdn.example/v.mp4"})]
    )

    media = await _service(session).resolve("https://vimeo.com/1", "video")

    assert media.url == "https://cdn.example/v.mp4"


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example/v.mp4",
        "http://cdn.example/v.mp4",
        "https://cdn.example/watch://not-a-scheme",
        "https://cobalt:9000/tunnel?id=AbCdEfGhIjKlMnOpQrStU",
    ],
)
async def test_a_link_that_is_already_fine_is_left_alone(url: str) -> None:
    """A working URL must survive the repair byte for byte.

    Includes the embedded instance's own tunnel shape, and a colon in the path —
    the first is what a self-hosted instance hands out, the second is where a
    simpler "strip the first colon" rule would have broken a good link.
    """
    session = FakeSession(post_responses=[FakeResponse(body={"status": "redirect", "url": url})])

    media = await _service(session).resolve("https://youtu.be/abc", "video")

    assert media.url == url


@pytest.mark.parametrize(
    ("url", "fetchable"),
    [
        ("https://cdn.example/v.mp4", True),
        ("http://cobalt:9000/tunnel?id=x", True),
        ("https:https://cdn.example/v.mp4", False),  # the shape the repair removes
        ("//cdn.example/v.mp4", False),
        ("ftp://cdn.example/v.mp4", False),
        ("cdn.example/v.mp4", False),
        ("", False),
    ],
)
def test_the_shape_check_judges_what_it_is_given(url: str, fetchable: bool) -> None:
    """smoke/boot_check rely on this being strict: a repaired link passes, a link
    nobody repaired fails at the *edge* instead of inside a user's transfer."""
    assert url_is_fetchable(url) is fetchable


async def test_the_repaired_link_is_what_gets_downloaded(tmp_path: Path) -> None:
    """The repair is worthless if the transfer still asks for the broken URL."""
    session = FakeSession(
        post_responses=[
            FakeResponse(body={"status": "redirect", "url": "https:https://cdn.example/v.mp4"})
        ],
        get_response=FakeResponse(headers={"Content-Type": "video/mp4"}, chunks=[b"x"]),
    )
    service = _service(session)
    media = await service.resolve("https://streamable.com/moo", "video")

    await service.download(media, tmp_path, max_bytes=1000)

    assert session.gets[0]["url"] == "https://cdn.example/v.mp4"


async def test_a_text_error_page_does_not_crash_the_parser() -> None:
    session = FakeSession(post_responses=[FakeResponse(status=502, body=None)])

    with pytest.raises(CobaltError) as caught:
        await _service(session).resolve("https://youtu.be/abc", "video")

    assert caught.value.code == "ERROR"


async def test_transport_trouble_becomes_a_cobalt_error() -> None:
    """Nothing from this module may surface as a raw aiohttp/OS exception."""

    def failing(exc: Exception) -> FakeSession:
        return FakeSession(error=exc)

    cases: list[tuple[Exception, str]] = [
        (aiohttp.ClientConnectionError("connection refused"), "UNREACHABLE"),
        (OSError("dns"), "UNREACHABLE"),
        (TimeoutError(), "TIMEOUT"),
    ]
    for exc, expected in cases:
        with pytest.raises(CobaltError) as caught:
            await _service(failing(exc)).resolve("https://youtu.be/abc", "video")
        assert caught.value.code == expected


async def test_without_a_url_the_fallback_is_simply_off() -> None:
    session = FakeSession()

    with pytest.raises(CobaltError) as caught:
        await CobaltService("", session=session).resolve("https://youtu.be/abc", "video")  # type: ignore[arg-type]

    assert caught.value.code == "DISABLED"
    assert session.posts == []


# ---------------------------------------------------------------------------
# The download
# ---------------------------------------------------------------------------


def _download_session(chunks: Iterable[bytes], **headers: str) -> FakeSession:
    return FakeSession(
        get_response=FakeResponse(
            headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": 'attachment; filename="Big Buck Bunny [id].mp4"',
                **headers,
            },
            chunks=chunks,
        )
    )


async def test_the_file_lands_in_the_job_directory(tmp_path: Path) -> None:
    session = _download_session([b"a" * 10, b"b" * 5])
    updates: list[dict[str, Any]] = []

    path = await _service(session).download(
        CobaltMedia(url="https://cdn.example/v.mp4"),
        tmp_path,
        max_bytes=1000,
        progress_hook=updates.append,
    )

    assert path.parent == tmp_path
    assert path.name == "Big Buck Bunny [id].mp4"
    assert path.read_bytes() == b"a" * 10 + b"b" * 5
    assert session.gets[0]["url"] == "https://cdn.example/v.mp4"
    # yt-dlp's shape, so the worker's progress editor needs no special case
    assert [update["status"] for update in updates] == ["downloading"] * 3
    assert updates[0]["downloaded_bytes"] == 10
    assert updates[-1]["downloaded_bytes"] == 15


async def test_an_rfc5987_filename_is_decoded(tmp_path: Path) -> None:
    session = FakeSession(
        get_response=FakeResponse(
            headers={
                "Content-Type": "video/mp4",
                "Content-Disposition": "attachment; filename*=UTF-8''%D9%81%DB%8C%D9%84%D9%85.mp4",
            },
            chunks=[b"x"],
        )
    )

    path = await _service(session).download(CobaltMedia(url="https://x/1"), tmp_path, max_bytes=10)

    assert path.name == "فیلم.mp4"


async def test_a_declared_size_over_the_ceiling_is_refused_before_writing(tmp_path: Path) -> None:
    session = _download_session([b"x" * 100], **{"Content-Length": "9000"})
    job = tmp_path / "job-1"

    with pytest.raises(CobaltError) as caught:
        await _service(session).download(CobaltMedia(url="https://x/1"), job, max_bytes=1000)

    assert caught.value.code == "TOO_LARGE"
    assert not job.exists(), "a refused job must not leave a directory behind"
    assert session.gets[0]["url"] == "https://x/1"


async def test_a_stream_that_outgrows_the_ceiling_is_cut_off(tmp_path: Path) -> None:
    """Sites lie about sizes; the ceiling is enforced on the bytes as they arrive."""
    session = _download_session([b"x" * 800, b"x" * 800])
    job = tmp_path / "job-1"

    with pytest.raises(CobaltError) as caught:
        await _service(session).download(CobaltMedia(url="https://x/1"), job, max_bytes=1000)

    assert caught.value.code == "TOO_LARGE"
    assert not job.exists()


async def test_an_empty_body_is_not_a_file(tmp_path: Path) -> None:
    session = _download_session([])
    job = tmp_path / "job-1"

    with pytest.raises(CobaltError) as caught:
        await _service(session).download(CobaltMedia(url="https://x/1"), job, max_bytes=1000)

    assert caught.value.code == "NO_MEDIA"
    assert not job.exists()


async def test_a_link_that_expired_says_so(tmp_path: Path) -> None:
    session = FakeSession(get_response=FakeResponse(status=410, chunks=[]))

    with pytest.raises(CobaltError) as caught:
        await _service(session).download(CobaltMedia(url="https://x/1"), tmp_path, max_bytes=10)

    assert caught.value.code == "UNREACHABLE"
    assert "410" in caught.value.message


async def test_a_missing_extension_comes_from_the_content_type(tmp_path: Path) -> None:
    session = FakeSession(
        get_response=FakeResponse(
            headers={"Content-Type": "audio/mpeg"},
            chunks=[b"x"],
        )
    )

    path = await _service(session).download(
        CobaltMedia(url="https://x/stream/1", filename="track"), tmp_path, max_bytes=10
    )

    assert path.name == "track.mp3"


async def test_a_nameless_download_still_gets_a_name(tmp_path: Path) -> None:
    session = FakeSession(get_response=FakeResponse(headers={"Content-Type": "video/webm"}, chunks=[b"x"]))

    path = await _service(session).download(CobaltMedia(url="https://x/"), tmp_path, max_bytes=10)

    assert path.name == "cobalt-fallback.webm"


async def test_a_progress_hook_that_raises_cannot_break_a_transfer(tmp_path: Path) -> None:
    session = _download_session([b"x"])

    def exploding(update: dict[str, Any]) -> None:
        raise RuntimeError("telegram is gone")

    path = await _service(session).download(
        CobaltMedia(url="https://x/1"), tmp_path, max_bytes=10, progress_hook=exploding
    )

    assert path.read_bytes() == b"x"


async def test_only_a_self_owned_session_is_closed() -> None:
    injected = FakeSession()
    service = _service(injected)

    await service.close()

    assert injected.closed is False
    assert service._session is None


# ---------------------------------------------------------------------------
# A picker: a post with several pictures (which is what an image tweet is)
# ---------------------------------------------------------------------------

#: The shape a multi-photo post comes back in — measured from cobalt's twitter
#: service, which builds exactly this for ``media.length > 1``.
PHOTO_PICKER = {
    "status": "picker",
    "picker": [
        {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg?name=4096x4096", "thumb": "https://cobalt/t/1"},
        {"type": "photo", "url": "https://pbs.twimg.com/media/b.jpg?name=4096x4096", "thumb": "https://cobalt/t/2"},
        {"type": "video", "url": "https://video.twimg.com/v.mp4", "thumb": "https://cobalt/t/3"},
    ],
}


def _image_response(filename: str, chunks: Iterable[bytes] = (b"jpeg",)) -> FakeResponse:
    return FakeResponse(
        headers={
            "Content-Type": "image/jpeg",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
        chunks=chunks,
    )


async def test_a_picker_keeps_every_item_in_the_post_s_order() -> None:
    session = FakeSession(post_responses=[FakeResponse(body=PHOTO_PICKER)])

    media = await _service(session).resolve("https://x.com/u/status/1", "video")

    assert [part.url for part in media.parts] == [
        "https://pbs.twimg.com/media/a.jpg?name=4096x4096",
        "https://pbs.twimg.com/media/b.jpg?name=4096x4096",
        "https://video.twimg.com/v.mp4",
    ]
    assert [part.kind for part in media.parts] == ["photo", "photo", "video"]
    assert media.url == "", "a set has no single link — the parts are the link"
    assert media.items == media.parts


def test_a_single_stream_is_still_one_item() -> None:
    media = CobaltMedia(url="https://cdn.example/v.mp4", size_bytes=1234)

    assert media.items == (
        CobaltPart(url="https://cdn.example/v.mp4", size_bytes=1234),
    )


async def test_a_picker_item_without_a_link_is_dropped() -> None:
    """It happens (an item whose url the service failed to build) and a gallery with
    a hole in it is worse than one picture fewer."""
    session = FakeSession(
        post_responses=[
            FakeResponse(
                body={
                    "status": "picker",
                    "picker": [{"type": "photo"}, {"type": "photo", "url": "  "}, "junk",
                               PHOTO_PICKER["picker"][0]],
                }
            )
        ]
    )

    media = await _service(session).resolve("https://x.com/u/status/1", "video")

    assert [part.url for part in media.parts] == ["https://pbs.twimg.com/media/a.jpg?name=4096x4096"]


async def test_every_picture_of_a_post_is_downloaded(tmp_path: Path) -> None:
    session = FakeSession(
        post_responses=[FakeResponse(body=PHOTO_PICKER)],
        get_responses=[
            _image_response("twitter_1.jpg"),
            _image_response("twitter_2.jpg"),
            FakeResponse(headers={"Content-Type": "video/mp4"}, chunks=(b"mp4",)),
        ],
    )
    service = _service(session)
    media = await service.resolve("https://x.com/u/status/1", "video")

    paths = await service.download_all(media, tmp_path, max_bytes=1000)

    assert [path.name for path in paths] == ["twitter_1.jpg", "twitter_2.jpg", "v.mp4"]
    assert [path.suffix for path in paths] == [".jpg", ".jpg", ".mp4"]
    assert [request["url"] for request in session.gets] == [part.url for part in media.parts]


async def test_two_items_with_one_name_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """Services reuse a filename per item; a gallery of one picture would be a bug
    that looks exactly like a working download."""
    session = FakeSession(
        post_responses=[FakeResponse(body={"status": "picker", "picker": [
            {"type": "photo", "url": "https://x/1.jpg"},
            {"type": "photo", "url": "https://x/1.jpg"},
        ]})],
        get_responses=[_image_response("same.jpg"), _image_response("same.jpg")],
    )
    service = _service(session)
    media = await service.resolve("https://x.com/u/status/1", "video")

    paths = await service.download_all(media, tmp_path, max_bytes=1000)

    assert [path.name for path in paths] == ["same.jpg", "same-2.jpg"]


async def test_a_gallery_that_fails_halfway_leaves_no_files_behind(tmp_path: Path) -> None:
    """Half an album is not the post the user asked for."""
    session = FakeSession(
        post_responses=[FakeResponse(body=PHOTO_PICKER)],
        get_responses=[
            _image_response("twitter_1.jpg"),
            FakeResponse(status=404, headers={}),
        ],
    )
    service = _service(session)
    media = await service.resolve("https://x.com/u/status/1", "video")

    with pytest.raises(CobaltError) as caught:
        await service.download_all(media, tmp_path, max_bytes=1000)

    assert caught.value.code == "UNREACHABLE"
    assert not tmp_path.exists(), "the job directory goes with the half gallery"


async def test_a_single_download_still_answers_with_one_path(tmp_path: Path) -> None:
    """``download`` is the old single-file entry point and must keep its contract."""
    session = _download_session([b"x"])

    path = await _service(session).download(
        CobaltMedia(url="https://cdn.example/v.mp4"), tmp_path, max_bytes=10
    )

    assert isinstance(path, Path)
    assert path.read_bytes() == b"x"
