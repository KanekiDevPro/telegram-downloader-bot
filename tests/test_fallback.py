"""The fallback path as the worker runs it: which failures it takes, and what the
user and the record see afterwards.

Two separate promises are pinned here. The user's: a blocked link is served, so it
arrives as a success and the status message says an alternative route is being
used. The operator's: the yt-dlp failure is *still recorded*, because a fallback
that hides a degraded primary engine would let the jar rot unnoticed — and a
fallback that also fails must leave the original diagnosis intact, not replace it
with something about engines the user never asked for.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from core.config import get_settings
from services import fallback, worker
from services.cobalt import CobaltError, CobaltMedia, CobaltPart
from services.delivery import join_file_ids
from services.extractor import (
    DownloadResult,
    ExtractionError,
    MediaInfo,
    SearchHit,
    classify_block,
    login_looking_block,
)
from services.fallback import FallbackUse
from services.queue import DownloadTask

TASK = DownloadTask(
    chat_id=5,
    telegram_id=5,
    url="https://www.youtube.com/watch?v=abc",
    media_format="video",
)

INFO = MediaInfo(
    source_url=TASK.url,
    title="A Clip",
    platform="youtube",
    webpage_url=TASK.url,
    extension="mp4",
    thumbnail=None,
    duration=95,
    filesize_approx=2048,
    is_live=False,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Status:
    """The editable status message the worker keeps updating."""

    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs: Any) -> None:
        self.edits.append(text)

    @property
    def last(self) -> str:
        return self.edits[-1] if self.edits else ""


class FakeBot:
    def __init__(self) -> None:
        self.status = Status()
        self.messages: list[tuple[int, str]] = []
        self.uploads: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Status:
        self.messages.append((chat_id, text))
        return self.status

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Any:
        self.uploads.append({"chat_id": chat_id, "kind": "photo", "caption": kwargs.get("caption", "")})
        return SimpleNamespace(photo=[SimpleNamespace(file_id="photo-1")])

    async def send_media_group(self, chat_id: int, media: Any, **kwargs: Any) -> Any:
        self.uploads.append(
            {
                "chat_id": chat_id,
                "kind": "group",
                "items": len(media),
                "caption": getattr(media[0], "caption", ""),
            }
        )
        return [
            SimpleNamespace(photo=[SimpleNamespace(file_id=f"photo-{index + 1}")])
            for index in range(len(media))
        ]

    async def send_video(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self.uploads.append({"chat_id": chat_id, "kind": "video", "caption": kwargs.get("caption", "")})
        return SimpleNamespace(video=SimpleNamespace(file_id="file-1"))

    async def send_document(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self.uploads.append({"chat_id": chat_id, "kind": "document", "caption": kwargs.get("caption", "")})
        return SimpleNamespace(document=SimpleNamespace(file_id="file-1"))

    async def send_audio(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self.uploads.append({"chat_id": chat_id, "kind": "audio", "caption": kwargs.get("caption", "")})
        return SimpleNamespace(audio=SimpleNamespace(file_id="file-1"))


class FakeExtractor:
    """Only what the worker touches: the two calls and the two attributes."""

    def __init__(
        self,
        *,
        download_dir: Path,
        cookie_file: Optional[Path] = None,
        extract_error: Optional[ExtractionError] = None,
        download_error: Optional[ExtractionError] = None,
        info: Optional[MediaInfo] = None,
        file_bytes: bytes = b"x" * 4096,
    ) -> None:
        self.download_dir = download_dir
        self.cookie_file = cookie_file
        self.extract_error = extract_error
        self.download_error = download_error
        self.info = info or INFO
        self.file_bytes = file_bytes
        self.downloads = 0
        #: What the primary engine was actually pointed at — a rewritten link has to
        #: reach it as the video it was rewritten *to*.
        self.extracted: list[str] = []
        self.downloaded: list[str] = []

    async def extract(self, url: str) -> MediaInfo:
        self.extracted.append(url)
        if self.extract_error is not None:
            raise self.extract_error
        return self.info

    async def download(self, url: str, media_format: str, progress_hook: Any = None) -> DownloadResult:
        self.downloads += 1
        self.downloaded.append(url)
        if self.download_error is not None:
            raise self.download_error
        job = self.download_dir / "job-primary"
        job.mkdir(parents=True, exist_ok=True)
        path = job / "primary.mp4"
        path.write_bytes(self.file_bytes)
        return DownloadResult(file_path=path, info=self.info, media_format="video")


class FakeCobalt:
    """Stands in for the HTTP client — the client itself is pinned in test_cobalt."""

    def __init__(
        self,
        *,
        failure: Optional[CobaltError] = None,
        enabled: bool = True,
        available: bool = True,
        quarantine_reason: str = "",
        media: Optional[CobaltMedia] = None,
        files: Optional[list[str]] = None,
    ) -> None:
        self.enabled = enabled
        self.available = available  # False = an instance being left alone (quarantined)
        self.quarantine_reason = quarantine_reason
        self.failure = failure
        #: What ``resolve`` answers, and what ``download_all`` writes — a photo post
        #: is a ``picker`` (several parts, several files), everything else is one.
        self.media = media or CobaltMedia(url="https://cdn.example/v.mp4")
        self.files = files or ["Big Buck Bunny [aqz-KE-bpKQ].mp4"]  # cobalt's "nerd" naming
        self.resolved: list[str] = []
        self.downloads = 0

    async def resolve(self, url: str, media_format: str) -> CobaltMedia:
        self.resolved.append(url)
        if self.failure is not None:
            raise self.failure
        return self.media

    async def download_all(
        self,
        media: CobaltMedia,
        target_dir: Path,
        *,
        max_bytes: int,
        progress_hook: Any = None,
    ) -> list[Path]:
        self.downloads += 1
        target_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for name in self.files:
            path = target_dir / name
            path.write_bytes(b"y" * 2048)
            paths.append(path)
        if progress_hook is not None:
            progress_hook({"status": "downloading", "downloaded_bytes": 2048, "total_bytes": 2048})
        return paths


class Env(SimpleNamespace):
    """Everything a test needs to inspect afterwards."""

    blocks: list[tuple[str, str]]
    memorized: list[dict[str, Any]]
    claims: int


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extract_error: Optional[ExtractionError] = None,
    download_error: Optional[ExtractionError] = None,
    cobalt: Optional[Any] = None,
    download_dir: Path,
    cookie_file: Optional[Path] = None,
    daily_limit_reached: bool = False,
) -> Env:
    """Wire the worker's collaborators to recorders (no DB, no network)."""
    blocks: list[tuple[str, str]] = []
    memorized: list[dict[str, Any]] = []
    claims = 0

    async def get_cached(pool: Any, url: str, media_format: str) -> None:
        return None

    async def memorize(pool: Any, **fields: Any) -> None:
        memorized.append(fields)

    async def get_user(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"telegram_id": telegram_id, "is_premium": True, "premium_until": None}

    async def can_claim(pool: Any, telegram_id: int, limit: int, today: Any) -> bool:
        nonlocal claims
        claims += 1
        return not daily_limit_reached

    async def record_block(pool: Any, task: DownloadTask, error: ExtractionError, jar: Any) -> None:
        from services.extractor import classify_block

        blocks.append((error.code, classify_block(error, task.url, jar)))

    monkeypatch.setattr(worker.cache_service, "get_cached", get_cached)
    monkeypatch.setattr(worker.cache_service, "memorize", memorize)
    monkeypatch.setattr(worker.database, "get_user", get_user)
    monkeypatch.setattr(worker.database, "can_claim_download", can_claim)
    monkeypatch.setattr(worker.telemetry, "record_block", record_block)

    extractor = FakeExtractor(
        download_dir=download_dir,
        cookie_file=cookie_file,
        extract_error=extract_error,
        download_error=download_error,
    )
    return Env(
        bot=FakeBot(),
        extractor=extractor,
        cobalt=_fake_cobalt() if cobalt is None else cobalt,
        pool=object(),
        blocks=blocks,
        memorized=memorized,
        claims=claims,
    )


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """``get_settings`` is cached, and the alert window is process-wide.

    A test that runs a real download path must not decide what an unrelated later
    test sees — that is how a suite starts depending on the machine's ``.env``.
    """
    get_settings.cache_clear()
    monkeypatch.setattr(worker, "_last_login_block_alert_at", float("-inf"))
    yield
    get_settings.cache_clear()


async def _run(env: Env) -> None:
    await worker.process_download_task(TASK, env.bot, env.pool, env.extractor, env.cobalt)


# ---------------------------------------------------------------------------
# Served by the fallback
# ---------------------------------------------------------------------------


async def test_a_blocked_extraction_is_served_by_the_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        download_dir=tmp_path,
    )
    env.claims = 0

    await _run(env)

    # The user got a file, not an error.
    assert [upload["kind"] for upload in env.bot.uploads] == ["video"]
    assert "Big Buck Bunny" in env.bot.uploads[0]["caption"]  # title from cobalt's filename
    assert "youtube" in env.bot.uploads[0]["caption"]
    assert env.bot.status.last.startswith("✅")
    assert "مسیر جایگزین" in " ".join(env.bot.status.edits)
    # ...and it is cached like any other download (with the *kind*, so a replay
    # knows which Telegram method to use).
    assert env.memorized == [
        {
            "url": TASK.url,
            "platform": "youtube",
            "telegram_file_id": "file-1",
            "quality": "video",
            "kind": "video",
        }
    ]


async def test_a_drm_refusal_is_served_by_the_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """yt-dlp will not touch a DRM site at all, so the net is the only route left —
    and the user must get the file, not the policy."""
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("DRM_PROTECTED", "DRM"),
        download_dir=tmp_path,
    )

    await _run(env)

    assert env.extractor.downloads == 0, "the primary engine never even tried"
    assert env.cobalt.resolved == [TASK.url]
    assert [upload["kind"] for upload in env.bot.uploads] == ["video"]
    assert "مسیر جایگزین" in " ".join(env.bot.status.edits)
    # Still written down as a degraded primary engine — the digest's "site" bucket.
    assert env.blocks == [("DRM_PROTECTED", "site")]


# ---------------------------------------------------------------------------
# An image post: the primary engine has no video to fetch, the net has pictures
# ---------------------------------------------------------------------------


IMAGE_TASK = DownloadTask(
    chat_id=5,
    telegram_id=5,
    url="https://x.com/user/status/12345",
    media_format="video",
)
IMAGE_ERROR = ExtractionError("IMAGE_ONLY", "No video could be found in this tweet")


def _photo_media(*urls: str) -> CobaltMedia:
    """What ``resolve`` answers for a post with several pictures."""
    return CobaltMedia(parts=tuple(CobaltPart(url=url, kind="photo") for url in urls))


async def _run_image_task(env: Env, task: DownloadTask = IMAGE_TASK) -> None:
    await worker.process_download_task(task, env.bot, env.pool, env.extractor, env.cobalt)


async def test_an_image_post_is_sent_as_a_photo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """«No video could be found in this tweet» is not a failure — it is a post whose
    content is a picture, and the user asked for the content."""
    env = _install(
        monkeypatch,
        extract_error=IMAGE_ERROR,
        cobalt=_fake_cobalt(files=["twitter_123.jpg"]),
        download_dir=tmp_path,
    )
    _record_uses(monkeypatch)

    await _run_image_task(env)

    assert [upload["kind"] for upload in env.bot.uploads] == ["photo"]
    # Cached as a photo, under the key of the format that was *asked* for — so the
    # next «video» request for the same link is answered from here.
    assert env.memorized == [
        {
            "url": IMAGE_TASK.url,
            "platform": "twitter",
            "telegram_file_id": "photo-1",
            "quality": "video",
            "kind": "photo",
        }
    ]
    assert env.blocks == [("IMAGE_ONLY", "site")], "the engine's gap is still on record"


async def test_a_post_with_several_pictures_arrives_as_one_album(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _install(
        monkeypatch,
        extract_error=IMAGE_ERROR,
        cobalt=_fake_cobalt(
            files=["twitter_1.jpg", "twitter_2.jpg"],
            media=_photo_media("https://pbs.twimg.com/1.jpg", "https://pbs.twimg.com/2.jpg"),
        ),
        download_dir=tmp_path,
    )
    _record_uses(monkeypatch)

    await _run_image_task(env)

    assert [(upload["kind"], upload["items"]) for upload in env.bot.uploads] == [("group", 2)]
    assert env.memorized == [
        {
            "url": IMAGE_TASK.url,
            "platform": "twitter",
            "telegram_file_id": join_file_ids(["photo-1", "photo-2"]),
            "quality": "video",
            "kind": "photo_group",
        }
    ]


async def test_a_mixed_post_is_delivered_and_not_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Photos and a video in one post is several messages of different kinds; one
    cache row cannot describe that, and replaying half of it would be worse."""
    env = _install(
        monkeypatch,
        extract_error=IMAGE_ERROR,
        cobalt=_fake_cobalt(files=["twitter_1.jpg", "twitter_1.mp4"]),
        download_dir=tmp_path,
    )
    _record_uses(monkeypatch)

    await _run_image_task(env)

    assert [upload["kind"] for upload in env.bot.uploads] == ["photo", "video"]
    assert env.memorized == []


async def test_an_image_arrives_as_a_photo_even_when_mp3_was_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The *file* decides how it is sent — a JPEG posted as «audio» would fail, and
    a document would be the wrong answer to a link that is a picture."""
    env = _install(
        monkeypatch,
        extract_error=IMAGE_ERROR,
        cobalt=_fake_cobalt(files=["twitter_123.jpg"]),
        download_dir=tmp_path,
    )
    _record_uses(monkeypatch)
    audio_task = replace(IMAGE_TASK, media_format="audio")

    await _run_image_task(env, audio_task)

    assert [upload["kind"] for upload in env.bot.uploads] == ["photo"]
    assert env.memorized[0]["quality"] == "audio", "the key is still what was asked for"
    assert env.memorized[0]["kind"] == "photo"


# ---------------------------------------------------------------------------
# Spotify: a link that is rewritten before either engine sees it
# ---------------------------------------------------------------------------


SPOTIFY_TASK = DownloadTask(
    chat_id=5,
    telegram_id=5,
    url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    media_format="video",
)
MAPPED_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _target() -> Any:
    """What ``spotify.youtube_target`` answers: a track and the video standing in."""
    from services.spotify import SpotifyTarget, SpotifyTrack

    track = SpotifyTrack(
        track_id="4uLU6hMCjMI75M1A2tKUQC",
        title="Never Gonna Give You Up",
        artists=("Rick Astley",),
        duration_s=213,
    )
    return SpotifyTarget(
        url=MAPPED_URL, track=track, hit=SearchHit(MAPPED_URL, "Official", 213)
    )


def _patch_mapping(monkeypatch: pytest.MonkeyPatch, *, error: Optional[ExtractionError] = None) -> list[str]:
    """Stand in for the Spotify lookup + search (pinned in test_spotify)."""
    asked: list[str] = []

    async def youtube_target(url: str, extractor: Any, **kwargs: Any) -> Any:
        asked.append(url)
        if error is not None:
            raise error
        return _target()

    monkeypatch.setattr(worker.spotify, "youtube_target", youtube_target)
    return asked


async def test_a_spotify_link_is_rewritten_before_anything_is_tried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neither engine can fetch Spotify, so the *link* is what changes — and the
    cache still belongs to the link the user sent (that is the instant second time)."""
    asked = _patch_mapping(monkeypatch)
    env = _install(monkeypatch, download_dir=tmp_path)

    await worker.process_download_task(SPOTIFY_TASK, env.bot, env.pool, env.extractor, env.cobalt)

    assert asked == [SPOTIFY_TASK.url]
    assert env.extractor.extracted == [MAPPED_URL]
    assert env.extractor.downloaded == [MAPPED_URL]
    assert "Rick Astley" in " ".join(env.bot.status.edits)
    assert env.memorized == [
        {
            "url": SPOTIFY_TASK.url,  # the user's link, not the stand-in
            "platform": "youtube",
            "telegram_file_id": "file-1",
            "quality": "video",
            "kind": "video",
        }
    ]


async def test_the_net_gets_the_mapped_video_not_the_spotify_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cobalt has no Spotify service, so handing it the original link would only
    spend a doomed round trip: by then the link *is* a YouTube video."""
    _patch_mapping(monkeypatch)
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        download_dir=tmp_path,
    )

    await worker.process_download_task(SPOTIFY_TASK, env.bot, env.pool, env.extractor, env.cobalt)

    assert env.cobalt.resolved == [MAPPED_URL]
    assert [upload["kind"] for upload in env.bot.uploads] == ["video"]


async def test_a_rewrite_that_failed_is_not_offered_to_the_net(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the *mapping* fails, there is nothing left that could work: the refusal
    (a blocked YouTube search, or a track that is not there) is the honest answer,
    and an attempt spent on a Spotify link teaches nobody anything."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    _patch_mapping(monkeypatch, error=error)
    env = _install(monkeypatch, download_dir=tmp_path)

    with pytest.raises(ExtractionError) as caught:
        await worker.process_download_task(SPOTIFY_TASK, env.bot, env.pool, env.extractor, env.cobalt)

    assert caught.value is error
    assert env.cobalt.resolved == []
    assert env.extractor.extracted == []


async def test_the_primary_engine_is_still_recorded_as_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of the telemetry: users are fine, the engine is not."""
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        download_dir=tmp_path,
    )

    await _run(env)

    assert env.blocks == [("EXTRACTOR_BLOCKED", "login")]


async def test_a_download_that_gets_blocked_midway_falls_back_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The retries are already exhausted when this error arrives — that is the case
    the fallback exists for, and the quota was claimed before it started."""
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    env = _install(
        monkeypatch,
        download_error=ExtractionError("SESSION_STALE", "stale"),
        download_dir=tmp_path,
        cookie_file=jar,
    )

    await _run(env)

    assert env.extractor.downloads == 1
    assert env.cobalt.downloads == 1
    assert [upload["kind"] for upload in env.bot.uploads] == ["video"]
    assert env.blocks == [("SESSION_STALE", "session")]


async def test_the_quota_is_claimed_once_per_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _install(
        monkeypatch,
        download_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        download_dir=tmp_path,
    )
    claims = 0

    async def counting(pool: Any, telegram_id: int, limit: int, today: Any) -> bool:
        nonlocal claims
        claims += 1
        return True

    monkeypatch.setattr(worker.database, "can_claim_download", counting)

    await _run(env)

    assert claims == 1


async def test_a_user_out_of_quota_does_not_get_a_fallback_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        download_dir=tmp_path,
        daily_limit_reached=True,
    )

    await _run(env)

    assert env.cobalt.downloads == 0
    assert env.bot.uploads == []
    assert "سهمیه" in env.bot.status.last
    assert env.blocks == [], "nothing was downloaded, so nothing should be reported"


# ---------------------------------------------------------------------------
# Not served, and how that reads
# ---------------------------------------------------------------------------


async def test_an_error_the_fallback_cannot_fix_is_not_handed_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A geo-block or a private video is the same answer from any address."""
    error = ExtractionError("GEO_RESTRICTED", "geo")
    env = _install(monkeypatch, extract_error=error, download_dir=tmp_path)

    with pytest.raises(ExtractionError) as caught:
        await _run(env)

    assert caught.value is error
    assert env.cobalt.resolved == []
    assert env.bot.uploads == []


async def test_without_a_configured_fallback_a_block_is_just_a_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(monkeypatch, extract_error=error, download_dir=tmp_path)
    env.cobalt = None

    with pytest.raises(ExtractionError) as caught:
        await _run(env)

    assert caught.value is error


async def test_a_quarantined_instance_is_not_asked_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An instance that just failed as an instance must not cost another wait."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(monkeypatch, extract_error=error, download_dir=tmp_path)
    env.cobalt = _fake_cobalt(available=False)

    with pytest.raises(ExtractionError) as caught:
        await _run(env)

    assert caught.value is error
    assert env.cobalt.resolved == [], "no doomed round trip while it is quarantined"


async def test_a_disabled_fallback_is_nobody_s_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(monkeypatch, extract_error=error, download_dir=tmp_path)
    env.cobalt = _fake_cobalt(enabled=False)

    with pytest.raises(ExtractionError) as caught:
        await _run(env)

    assert caught.value is error
    assert env.cobalt.resolved == []


async def test_a_fallback_that_fails_too_keeps_the_original_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The user's message must be about their link, not about our engines."""
    error = ExtractionError("EXTRACTOR_BLOCKED", "blocked")
    env = _install(
        monkeypatch,
        extract_error=error,
        cobalt=_fake_cobalt(failure=CobaltError("UNREACHABLE", "instance is down")),
        download_dir=tmp_path,
    )

    with pytest.raises(ExtractionError) as caught:
        await _run(env)

    assert caught.value is error
    assert fallback.fallback_was_attempted(caught.value)
    assert env.blocks == [], "the failure path records it once, not twice"
    assert env.bot.uploads == []


# ---------------------------------------------------------------------------
# What gets written down about the net
# ---------------------------------------------------------------------------


def _record_uses(monkeypatch: pytest.MonkeyPatch) -> list[FallbackUse]:
    """Capture the rows the worker writes about the net (the ``bot_state`` table)."""
    uses: list[FallbackUse] = []

    async def set_state(pool: Any, key: str, value: str) -> None:
        if key == fallback.USE_STATE_KEY:
            parsed = FallbackUse.parse(value)
            assert parsed is not None, value
            uses.append(parsed)

    async def get_state(pool: Any, key: str) -> Optional[str]:
        return None

    monkeypatch.setattr(fallback.database, "set_state", set_state)
    monkeypatch.setattr(fallback.database, "get_state", get_state)
    return uses


async def test_a_net_that_was_never_asked_is_recorded_as_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one fact the logs hide: the link was blocked, and the net did not even
    get a chance — so the next /blocks can say why without a probe."""
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        cobalt=_fake_cobalt(available=False, quarantine_reason="ERROR: instance is down"),
        download_dir=tmp_path,
    )
    uses = _record_uses(monkeypatch)

    with pytest.raises(ExtractionError):
        await _run(env)

    assert [use.outcome for use in uses] == [fallback.USE_SKIPPED]
    assert uses[0].reason == "قرنطینه بود (ERROR: instance is down)"
    assert not uses[0].ok


async def test_a_net_that_tried_and_failed_is_recorded_as_such(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        cobalt=_fake_cobalt(failure=CobaltError("UNREACHABLE", "instance is down")),
        download_dir=tmp_path,
    )
    uses = _record_uses(monkeypatch)

    with pytest.raises(ExtractionError):
        await _run(env)

    assert [use.outcome for use in uses] == [fallback.USE_FAILED]
    assert uses[0].reason == "UNREACHABLE: instance is down"


async def test_a_link_the_net_served_is_recorded_as_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("EXTRACTOR_BLOCKED", "blocked"),
        download_dir=tmp_path,
    )
    uses = _record_uses(monkeypatch)

    await _run(env)

    assert [use.outcome for use in uses] == [fallback.USE_USED]
    assert uses[0].ok
    assert uses[0].reason == ""


async def test_a_failure_the_net_was_never_for_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A private video is not a health problem, and must not be filed as one."""
    env = _install(
        monkeypatch,
        extract_error=ExtractionError("PRIVATE_VIDEO", "private"),
        download_dir=tmp_path,
    )
    uses = _record_uses(monkeypatch)

    with pytest.raises(ExtractionError):
        await _run(env)

    assert uses == []


async def test_both_engines_are_not_re_run_three_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocked address does not recover in the two seconds before a retry."""
    attempts = 0
    recorded: list[tuple[str, str]] = []

    async def failing_process(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise fallback.mark_fallback_attempted(
            ExtractionError("EXTRACTOR_BLOCKED", "blocked")
        )

    async def record_block(pool: Any, task: DownloadTask, error: ExtractionError, jar: Any) -> None:
        recorded.append((error.code, task.url))

    async def refresh(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker, "process_download_task", failing_process)
    monkeypatch.setattr(worker.telemetry, "record_block", record_block)
    monkeypatch.setattr(worker.telemetry, "maybe_send_early_alert", lambda *a, **k: _done())
    monkeypatch.setattr(worker.cookie_refresh, "auto_refresh_jar", refresh)

    bot = FakeBot()
    await worker._process_with_retry(
        TASK,
        bot,  # type: ignore[arg-type]
        object(),
        _Queue(),  # type: ignore[arg-type]  # only ``requeue`` is ever called
        FakeExtractor(download_dir=tmp_path),  # type: ignore[arg-type]
        _NeverStopping(),  # type: ignore[arg-type]  # only ``is_set`` is ever read
    )

    assert attempts == 1
    assert recorded == [("EXTRACTOR_BLOCKED", TASK.url)]


async def _done() -> None:
    return None


class _NeverStopping:
    """An asyncio.Event-shaped stop flag that is never set (no sleeps happen)."""

    def is_set(self) -> bool:
        return False


class _Queue:
    async def requeue(self, task: DownloadTask) -> None:
        return None


# ---------------------------------------------------------------------------
# The pieces the worker relies on
# ---------------------------------------------------------------------------


def _fake_cobalt(**kwargs: Any) -> Any:
    """Typed as ``Any`` on purpose: the fakes stand in for a real client here."""
    return FakeCobalt(**kwargs)


def test_only_the_refusals_another_engine_can_answer_are_handed_over() -> None:
    service = _fake_cobalt()

    assert fallback.should_use_fallback(ExtractionError("EXTRACTOR_BLOCKED", ""), service)
    assert fallback.should_use_fallback(ExtractionError("SESSION_STALE", ""), service)
    # A DRM verdict is yt-dlp's policy, not our address — and another engine
    # (Cobalt serves several of those sites through a different service) may
    # still have an answer, so it is worth exactly one attempt.
    assert fallback.should_use_fallback(ExtractionError("DRM_PROTECTED", ""), service)
    # An image post: the primary engine serves video, the fallback serves pictures.
    assert fallback.should_use_fallback(ExtractionError("IMAGE_ONLY", ""), service)
    assert not fallback.should_use_fallback(ExtractionError("PRIVATE_VIDEO", ""), service)
    assert not fallback.should_use_fallback(ExtractionError("LIVE_STREAM", ""), service)
    assert not fallback.should_use_fallback(ExtractionError("TIMEOUT", ""), service)
    assert not fallback.should_use_fallback(ExtractionError("EXTRACTOR_BLOCKED", ""), None)
    assert not fallback.should_use_fallback(ExtractionError("DRM_PROTECTED", ""), None)
    assert not fallback.should_use_fallback(ExtractionError("IMAGE_ONLY", ""), None)


def test_a_drm_refusal_is_not_a_block_for_the_record() -> None:
    """The digest must not count a DRM site as a login or IP block."""
    error = ExtractionError("DRM_PROTECTED", "DRM")

    assert classify_block(error, "https://open.spotify.com/track/1", None) == "site"
    assert not login_looking_block(error, "https://open.spotify.com/track/1", None)


def test_the_platform_name_reads_like_a_person_wrote_it() -> None:
    assert fallback.platform_for("https://youtu.be/abc") == "youtube"
    assert fallback.platform_for("https://www.youtube.com/watch?v=1") == "youtube"
    assert fallback.platform_for("https://x.com/u/status/1") == "twitter"
    assert fallback.platform_for("https://www.instagram.com/reel/1/") == "instagram"
    assert fallback.platform_for("https://www.dailymotion.com/video/x") == "dailymotion"
    assert fallback.platform_for("https://example.org/v/1") == "example"
    assert fallback.platform_for("not a url") == "unknown"


def test_the_title_comes_from_the_file_the_fallback_named(tmp_path: Path) -> None:
    named = tmp_path / "Big Buck Bunny [aqz-KE-bpKQ].mp4"
    named.write_bytes(b"x")

    assert fallback.title_for(TASK.url, named) == "Big Buck Bunny"
    # A generated name says nothing, so the link is the more honest label.
    assert "abc" in fallback.title_for(TASK.url, tmp_path / "cobalt-fallback.mp4")


def test_a_fallback_failure_is_described_with_its_code() -> None:
    described = fallback.describe(CobaltError("UNREACHABLE", "instance is down"))

    assert described == "UNREACHABLE: instance is down"
