"""Video metadata on delivery: the inline player needs dimensions and duration.

A video sent without ``width``/``height``/``duration`` still arrives, but the
client shows a black frame with no scrub bar — the metadata is what turns the
attachment into a *player*. The probe in ``services.verify`` already measures
exactly these numbers (one ffprobe run per delivery); this pins that the
measured values reach ``send_video`` — with no second probe — and that only
measured, positive integers are ever passed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from core.config import Settings
from services import worker
from services.verify import MediaFacts


class FakeBot:
    """Records the kwargs each send method was handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def send_video(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_video", file, kwargs))
        return SimpleNamespace(video=SimpleNamespace(file_id="vid-1"))

    async def send_document(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self.calls.append(("send_document", file, kwargs))
        return SimpleNamespace(document=SimpleNamespace(file_id="doc-1"))


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        BOT_TOKEN="1:2",
        QUEUE_BACKEND="memory",
        WORKER_COUNT=0,
    )


def _media(tmp_path: Path, name: str = "clip.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"x" * 32)
    return path


def _facts(**over: Any) -> MediaFacts:
    base: dict[str, Any] = {
        "format_name": "mov,mp4",
        "codec": "h264",
        "width": 1280,
        "height": 720,
        "duration_s": 61.5,
        "media_streams": 1,
    }
    base.update(over)
    return MediaFacts(**base)


async def test_send_video_carries_the_measured_dimensions_and_duration(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The probe's width/height/duration reach send_video — no second probe."""
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    bot = FakeBot()
    media = _media(tmp_path)

    file_id = await worker._send_file(
        bot, 1, media, "video", "caption", facts=_facts()  # type: ignore[arg-type]
    )

    assert file_id == "vid-1"
    assert bot.calls[0][0] == "send_video"
    kwargs = bot.calls[0][2]
    assert kwargs["width"] == 1280
    assert kwargs["height"] == 720
    assert kwargs["duration"] == 61
    assert kwargs["supports_streaming"] is True


async def test_send_video_without_facts_sends_exactly_what_it_always_did(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """No probe (ffprobe missing, photo-first mixed post) means no new keys."""
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    bot = FakeBot()
    media = _media(tmp_path)

    await worker._send_file(bot, 1, media, "video", "caption")  # type: ignore[arg-type]

    assert bot.calls[0][0] == "send_video"
    assert set(bot.calls[0][2]) == {"caption", "supports_streaming"}


async def test_only_measured_positive_integers_are_passed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Unreported (None), zero or negative measurements stay off the call —
    Telegram would reject a width of 0, and a guess is worse than silence."""
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    bot = FakeBot()
    media = _media(tmp_path)

    await worker._send_file(
        bot, 1, media, "video", "caption",  # type: ignore[arg-type]
        facts=_facts(width=None, height=0, duration_s=-3.0),
    )

    assert bot.calls[0][0] == "send_video"
    assert set(bot.calls[0][2]) == {"caption", "supports_streaming"}


def test_the_kwargs_builder_passes_only_what_was_measured() -> None:
    assert worker._video_send_kwargs(None) == {}
    assert worker._video_send_kwargs(_facts()) == {"width": 1280, "height": 720, "duration": 61}
    assert worker._video_send_kwargs(_facts(width=0, height=-1, duration_s=0.0)) == {}
    assert worker._video_send_kwargs(_facts(duration_s=59.9))["duration"] == 59
    # A hand-built fact (or an audio probe) without video dimensions:
    assert worker._video_send_kwargs(MediaFacts(codec="mp3")) == {}


async def test_the_document_fallback_warns_with_url_suffix_and_telegram_error(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """A video Telegram refuses *as a video* still arrives — but silently no
    longer: the warning names the source URL, the file suffix and Telegram's
    own error, which is the whole diagnosis for an unplayable container."""
    from aiogram.exceptions import TelegramBadRequest

    monkeypatch.setattr(worker, "get_settings", lambda: _settings())

    class RefusingBot(FakeBot):
        async def send_video(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
            self.calls.append(("send_video", file, kwargs))
            raise TelegramBadRequest(
                method=None,  # type: ignore[arg-type]
                message="Bad Request: wrong file identifier",
            )

    bot = RefusingBot()
    # A *playable* container Telegram still refuses: the genuinely unexpected
    # fallback (an unplayable .mkv never attempts the video send at all — F4).
    media = _media(tmp_path, 'clip.mp4')

    with caplog.at_level("WARNING", logger="services.worker"):
        file_id = await worker._send_file(
            bot, 1, media, "video", "caption", facts=_facts()  # type: ignore[arg-type]
        )

    assert file_id == "doc-1"
    assert [call[0] for call in bot.calls] == ["send_video", "send_document"]
    assert ".mp4" in caplog.text, "the file suffix is the diagnosis"
    assert "wrong file identifier" in caplog.text, "Telegram's own error goes in verbatim"


# ---------------------------------------------------------------------------
# F4: unplayable containers route to document deliberately
# ---------------------------------------------------------------------------


def test_delivery_kind_routes_unplayable_video_containers_to_document() -> None:
    """The kind is the routing decision: .mp4/.webm play inline, everything
    else a video request produces (.mkv from the old merge preference or a
    foreign engine, .avi/.mov/.wmv) is deliberately a document — never a
    video send Telegram will refuse."""
    for suffix in (".mkv", ".avi", ".mov", ".wmv"):
        assert worker._delivery_kind(Path(f"clip{suffix}"), "video") == "file", suffix
    assert worker._delivery_kind(Path("clip.mp4"), "video") == "video"
    assert worker._delivery_kind(Path("clip.webm"), "video") == "video"
    assert worker._delivery_kind(Path("clip.MP4"), "video") == "video"
    # Untouched: audio keeps its method whatever the suffix, photos stay photos.
    assert worker._delivery_kind(Path("song.mkv"), "audio") == "audio"
    assert worker._delivery_kind(Path("pic.jpg"), "video") == "photo"


async def test_an_unplayable_container_is_sent_as_a_document_without_a_doomed_video_attempt(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    """No send_video attempt Telegram is known to refuse: straight to document,
    with a warning that names the deliberate routing (the F3 warning stays for
    the genuinely unexpected refusals)."""
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    bot = FakeBot()
    media = _media(tmp_path, "clip.mkv")

    with caplog.at_level("WARNING", logger="services.worker"):
        file_id = await worker._send_file(
            bot,  # type: ignore[arg-type]
            1,
            media,
            "video",
            "caption",
            facts=_facts(),
            source_url="https://example.com/v",
        )

    assert file_id == "doc-1"
    assert [call[0] for call in bot.calls] == ["send_document"]
    assert ".mkv" in caplog.text
    assert "https://example.com/v" in caplog.text
