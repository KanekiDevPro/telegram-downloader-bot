"""Zero-copy upload through a local Bot API server: the file URI, and the fallback.

Production telemetry said ``upload=30.1s`` for a 460 MB file — the bytes streamed
over the localhost HTTP socket at a crawl while the local ``telegram-bot-api``
server could have read the very same file off the shared volume. The Bot API
contract for a local server allows exactly that ("Upload files using their local
path and the file URI scheme" — core.telegram.org/bots/api), so when the
deployment maps the job directory into the server *at the same path*, an upload
becomes a metadata call: no bytes cross any socket.

Two halves are pinned here: the URI is used only where it is safe (local server,
local mode, shared volume, file inside it), and a server that refuses the URI
still gets its bytes streamed — every existing error mapping and fallback route
stays exactly as it was.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto

from core.config import Settings
from services import worker


class FakeBot:
    """Records the exact ref each method was handed; can refuse file URIs."""

    def __init__(self, *, refuse_uris: bool = False, fail_video: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.refuse_uris = refuse_uris
        self.fail_video = fail_video

    def _accept(self, method: str, ref: Any) -> None:
        self.calls.append((method, ref))
        if self.refuse_uris and isinstance(ref, str):
            raise self._bad_request()
        if self.fail_video and method == "send_video":
            raise self._bad_request()

    @staticmethod
    def _bad_request() -> TelegramBadRequest:
        return TelegramBadRequest(method=cast("Any", None), message="Bad Request: wrong file identifier")

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    @property
    def refs(self) -> list[Any]:
        return [ref for _, ref in self.calls]

    async def send_document(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self._accept("send_document", file)
        return SimpleNamespace(document=SimpleNamespace(file_id="doc-1"))

    async def send_video(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self._accept("send_video", file)
        return SimpleNamespace(video=SimpleNamespace(file_id="vid-1"))

    async def send_audio(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self._accept("send_audio", file)
        return SimpleNamespace(audio=SimpleNamespace(file_id="aud-1"))

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Any:
        self._accept("send_photo", photo)
        return SimpleNamespace(photo=[SimpleNamespace(file_id="ph-1")])

    async def send_media_group(self, chat_id: int, media: Any) -> Any:
        self.calls.append(("send_media_group", media))
        if self.refuse_uris and any(isinstance(item.media, str) for item in media):
            raise self._bad_request()
        return [SimpleNamespace(photo=[SimpleNamespace(file_id=f"ph-{i}")]) for i in range(len(media))]


def _settings(shared: Path | None) -> Settings:
    env: dict[str, Any] = {
        "BOT_TOKEN": "1:2",
        "QUEUE_BACKEND": "memory",
        "WORKER_COUNT": 0,
        "TELEGRAM_API_BASE_URL": "http://telegram-api:8081",
        "TELEGRAM_API_LOCAL": True,
    }
    if shared is not None:
        env["TELEGRAM_API_SHARED_DIR"] = shared
    return Settings(_env_file=None, **env)  # type: ignore[call-arg]


def _media(tmp_path: Path, name: str = "video.mp4") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 32)
    return path


def _use(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: settings)


async def test_a_recovered_local_server_takes_the_zero_copy_path_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback latch is not a verdict. A server that was down at boot (and
    latched the cloud fallback) and comes back must get its zero-copy path back
    on the next delivery — the send path re-checks the server (rate-limited),
    clears the latch, and hands over the URI instead of streaming the bytes."""
    shared = tmp_path / "shared"
    shared.mkdir()
    settings = _settings(shared)
    settings.use_cloud_api_fallback()
    _use(monkeypatch, settings)

    async def healthy(_settings: Settings) -> bool:
        return True

    monkeypatch.setattr(worker, "local_api_is_reachable", healthy)
    monkeypatch.setattr(worker, "_recovery_probe_at", 0.0)
    media = _media(shared / "job-1")
    bot = FakeBot()

    await worker._send_document(bot, 1, media, "caption")  # type: ignore[arg-type]

    assert isinstance(bot.refs[0], str), "the recovered server gets the URI again"
    assert settings.cloud_api_fallback is False


async def test_the_upload_is_a_file_uri_not_a_stream_of_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: the server reads the file itself; nothing is uploaded."""
    shared = tmp_path / "shared"
    media = _media(shared / "job-1")
    _use(monkeypatch, _settings(shared))
    bot = FakeBot()

    file_id = await worker._send_document(bot, 1, media, "caption")  # type: ignore[arg-type]

    assert file_id == "doc-1"
    assert bot.methods == ["send_document"]
    assert bot.refs == [media.resolve().as_uri()], "the path the server reads, as a file URI"


async def test_a_file_outside_the_shared_volume_is_still_streamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server cannot see what the volume does not carry — stream those."""
    shared = tmp_path / "shared"
    shared.mkdir()
    outside = _media(tmp_path / "elsewhere")
    _use(monkeypatch, _settings(shared))
    bot = FakeBot()

    await worker._send_document(bot, 1, outside, "caption")  # type: ignore[arg-type]

    assert bot.methods == ["send_document"]
    assert isinstance(bot.refs[0], FSInputFile)


async def test_without_a_shared_volume_the_upload_is_exactly_what_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _settings(None))
    bot = FakeBot()
    media = _media(tmp_path)

    await worker._send_document(bot, 1, media, "caption")  # type: ignore[arg-type]

    assert bot.methods == ["send_document"]
    assert isinstance(bot.refs[0], FSInputFile)


async def test_a_refused_file_uri_falls_back_to_streaming_the_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that will not read the URI still delivers — the safety net that
    keeps a misconfigured volume from ever failing a user's link."""
    shared = tmp_path / "shared"
    media = _media(shared / "job-1")
    _use(monkeypatch, _settings(shared))
    bot = FakeBot(refuse_uris=True)

    file_id = await worker._send_document(bot, 1, media, "caption")  # type: ignore[arg-type]

    assert file_id == "doc-1"
    assert bot.methods == ["send_document", "send_document"]
    assert isinstance(bot.refs[1], FSInputFile), "the second attempt carries real bytes"


async def test_a_refused_video_falls_back_to_a_document_like_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old error route survives the new fast path untouched: a video
    Telegram refuses *as a video* still arrives as a document."""
    shared = tmp_path / "shared"
    media = _media(shared / "job-1")
    _use(monkeypatch, _settings(shared))
    bot = FakeBot(fail_video=True)

    file_id = await worker._send_file(bot, 1, media, "video", "caption")  # type: ignore[arg-type]

    assert file_id == "doc-1"
    assert bot.methods == ["send_video", "send_video", "send_document"]
    assert isinstance(bot.refs[1], FSInputFile), "the URI is retried as bytes before the document"
    assert isinstance(bot.refs[2], str), "the document keeps its own zero-copy attempt"


async def test_the_audio_upload_travels_the_same_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    media = _media(shared / "job-1", "song.m4a")
    _use(monkeypatch, _settings(shared))
    bot = FakeBot()

    file_id = await worker._send_file(bot, 1, media, "audio", "caption")  # type: ignore[arg-type]

    assert file_id == "aud-1"
    assert bot.refs == [media.resolve().as_uri()]


async def test_a_photo_album_uses_the_uri_for_every_picture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    images = [_media(shared / "job-1", f"pic-{index}.jpg") for index in range(2)]
    _use(monkeypatch, _settings(shared))
    bot = FakeBot()

    delivered = await worker._send_photos(bot, 1, images, "caption")  # type: ignore[arg-type]

    assert delivered.kind == "photo_group"
    assert bot.methods == ["send_media_group"]
    media = bot.calls[0][1]
    assert all(isinstance(item, InputMediaPhoto) for item in media)
    assert [item.media for item in media] == [image.resolve().as_uri() for image in images]


async def test_a_refused_album_falls_back_to_streamed_pictures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = tmp_path / "shared"
    images = [_media(shared / "job-1", f"pic-{index}.jpg") for index in range(2)]
    _use(monkeypatch, _settings(shared))
    bot = FakeBot(refuse_uris=True)

    delivered = await worker._send_photos(bot, 1, images, "caption")  # type: ignore[arg-type]

    assert delivered.kind == "photo_group"
    assert bot.methods == ["send_media_group", "send_media_group"]
    second = bot.calls[1][1]
    assert all(isinstance(item.media, FSInputFile) for item in second)


async def test_the_refusal_log_names_the_daemon_s_own_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """"refused the file URI" alone sent nobody anywhere: "wrong file identifier"
    means the daemon never ran in local mode, "can't open" means the mount or the
    permissions — the message is the diagnosis, so it goes in the log verbatim."""
    shared = tmp_path / "shared"
    media = _media(shared / "job-1", "فقط ۴ سال! [icAb-wGW-4w].mp4")
    _use(monkeypatch, _settings(shared))
    bot = FakeBot(refuse_uris=True)

    with caplog.at_level("INFO"):
        await worker._send_document(bot, 1, media, "caption")  # type: ignore[arg-type]

    assert "Bad Request: wrong file identifier" in caplog.text


async def test_the_daemon_is_handed_readable_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bot writes as uid 10001; the telegram-bot-api daemon reads as uid
    1000 (the image's own user). A tighter umask would end a zero-copy upload as
    a refused URI with no clue beyond "can't open file", so the job directory is
    made traversable and the file readable before the URI is ever issued —
    best-effort: a filesystem that refuses chmod must not fail the delivery."""
    shared = tmp_path / "shared"
    media = _media(shared / "job-1", "song.m4a")
    _use(monkeypatch, _settings(shared))
    chmods: list[tuple[str, int]] = []
    monkeypatch.setattr(Path, "chmod", lambda self, mode: chmods.append((str(self), mode)))
    bot = FakeBot()

    await worker._send_document(bot, 1, media, "caption")  # type: ignore[arg-type]

    assert (str(media.parent), 0o755) in chmods, "the daemon must be able to enter the job dir"
    assert (str(media), 0o644) in chmods, "and to open the file itself"
    assert chmods.index((str(media.parent), 0o755)) < chmods.index((str(media), 0o644))
    assert isinstance(bot.refs[0], str), "the readable path is the one that gets the URI"
