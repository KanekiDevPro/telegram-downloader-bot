"""A cached replay must be visually identical to the fresh send it remembers.

The cache row now carries the media's title (a backward-compatible column: an
older row simply has none), so the replayed card says 🎬 just like the first
delivery. What it must never do is invent one — an old row omits the line, and
the URL is never dressed up as a title.
"""
from __future__ import annotations

from typing import Any

from services import cache as cache_service
from services.delivery import label_for_request, media_card, replay_caption

EN = "en"

URL = "https://www.youtube.com/watch?v=abc"
#: The request exactly as the pipeline spells it — the key is built from this.
REQUEST = cache_service.request_key("video", "360p")


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "telegram_file_id": "f1",
        "quality": REQUEST,
        "original_url": URL,
        "kind": "video",
        "title": "A Clip",
    }
    row.update(over)
    return row


def test_a_replay_shows_the_title_it_remembered() -> None:
    caption = replay_caption(_row(), EN)
    assert "🎬 A Clip" in caption
    assert f"🔗 {URL}" in caption


def test_the_same_card_shows_up_whether_fresh_or_cached() -> None:
    cached = replay_caption(_row(), EN)
    fresh = media_card(
        title="A Clip",
        url=URL,
        quality=label_for_request(REQUEST, EN),
        audio=False,
        lang=EN,
    )
    assert cached == fresh


def test_a_replay_shows_the_label_the_fresh_caption_used() -> None:
    caption = replay_caption(
        _row(quality="audio:flac", label="🎧 FLAC · 41.2 MB"), EN
    )
    assert "🎧 FLAC · 41.2 MB" in caption
    # The request's own wording never shows through once a label exists.
    assert "MP3" not in caption


def test_an_old_row_without_a_title_still_replays_safely() -> None:
    row = _row()
    row.pop("title")
    caption = replay_caption(row, EN)
    assert "🎬" not in caption
    assert "None" not in caption
    assert caption.splitlines()[0] != URL, "the URL must never be the title"


def test_a_null_title_is_treated_as_no_title() -> None:
    caption = replay_caption(_row(title=None), EN)
    assert "🎬" not in caption
    assert "None" not in caption


async def test_a_fresh_row_remembers_the_title(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_store(pool: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cache_service.database, "store_cached_file", fake_store)
    await cache_service.memorize(
        object(),
        url=URL,
        platform="youtube",
        telegram_file_id="f1",
        request=REQUEST,
        kind="video",
        title="A Clip",
    )
    assert captured["title"] == "A Clip"
    assert captured["url_hash"] == cache_service.cache_key(URL, "video", "360p")
    assert captured["quality"] == REQUEST


async def test_the_title_never_touches_the_cache_key(monkeypatch: Any) -> None:
    """Schema addition only: keys, lookups and old rows are all untouched."""
    hashes: list[str] = []

    async def fake_store(pool: Any, **kwargs: Any) -> None:
        hashes.append(kwargs["url_hash"])

    monkeypatch.setattr(cache_service.database, "store_cached_file", fake_store)
    for title in ("A Clip", ""):
        await cache_service.memorize(
            object(),
            url=URL,
            platform="youtube",
            telegram_file_id="f1",
            request=REQUEST,
            title=title,
        )
    assert hashes[0] == hashes[1] == cache_service.cache_key(URL, "video", "360p")
