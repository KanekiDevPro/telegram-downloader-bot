"""Sending a cached file back: the cache row decides *how*, not the request.

An image post answers a «video» ask with photos, so the row remembers what the upload
actually was (``kind``) — and a row written before that column existed must still be
delivered the way it always was, not as a document. A gallery keeps several ids in the
one column cache rows have, which is the part with a shape worth pinning: an album
that replays as a single picture would be a bug that looks like a working cache.
"""

from __future__ import annotations

from typing import Any, cast

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InputMediaPhoto

from services import delivery

GROUP_MAX = delivery.MEDIA_GROUP_MAX


def _bad_request(message: str = "Bad Request: wrong file identifier") -> TelegramBadRequest:
    return TelegramBadRequest(method=cast("Any", None), message=message)


class FakeBot:
    """Records which sending method was used, and with what."""

    def __init__(self, *, failing: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.failing = set(failing)

    def _note(self, method: str, payload: Any) -> None:
        if method in self.failing:
            raise _bad_request()
        self.calls.append((method, payload))

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Any:
        self._note("send_photo", photo)

    async def send_media_group(self, chat_id: int, media: Any) -> Any:
        self._note("send_media_group", media)
        return [object() for _ in media]

    async def send_video(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self._note("send_video", file)

    async def send_audio(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self._note("send_audio", file)

    async def send_document(self, chat_id: int, file: Any, **kwargs: Any) -> Any:
        self._note("send_document", file)

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def _row(**fields: Any) -> dict[str, Any]:
    """A cache row. ``kind`` is absent for rows written before the column existed."""
    return {
        "url_hash": "h",
        "telegram_file_id": "AgAC-photo",
        "quality": "video",
        "original_url": "https://youtu.be/abc",
        "title": "A Clip",
        **fields,
    }


# ---------------------------------------------------------------------------
# What the row says
# ---------------------------------------------------------------------------


def test_the_kinds_survive_the_one_column_they_travel_in() -> None:
    assert delivery.join_file_ids(["a", "b"]) == '["a", "b"]'
    assert delivery.split_file_ids('["a", "b"]') == ["a", "b"]
    assert delivery.split_file_ids("AgAC-one") == ["AgAC-one"]  # a single id is not a list
    assert delivery.split_file_ids("") == []


def test_an_unreadable_id_list_is_not_a_crash() -> None:
    assert delivery.split_file_ids("[not json") == []
    assert delivery.split_file_ids("[1, 2]") == [], "only ids, nothing else"


async def test_a_photo_row_is_sent_as_a_photo() -> None:
    bot = FakeBot()

    assert await delivery.send_cached_file(bot, 1, _row(kind="photo"))  # type: ignore[arg-type]

    assert bot.methods == ["send_photo"]


async def test_a_gallery_row_is_sent_as_one_album() -> None:
    bot = FakeBot()
    row = _row(kind="photo_group", telegram_file_id=delivery.join_file_ids(["a", "b", "c"]))

    assert await delivery.send_cached_file(bot, 1, row)  # type: ignore[arg-type]

    assert bot.methods == ["send_media_group"]
    media = bot.calls[0][1]
    assert [item.media for item in media] == ["a", "b", "c"]
    assert media[0].caption, "the caption rides on the first photo"
    assert all(isinstance(item, InputMediaPhoto) for item in media)


async def test_an_album_larger_than_telegram_allows_is_batched() -> None:
    bot = FakeBot()
    ids = [f"id-{index}" for index in range(GROUP_MAX + 2)]
    row = _row(kind="photo_group", telegram_file_id=delivery.join_file_ids(ids))

    assert await delivery.send_cached_file(bot, 1, row)  # type: ignore[arg-type]

    assert bot.methods == ["send_media_group", "send_media_group"]
    assert [len(batch) for _, batch in bot.calls] == [GROUP_MAX, 2]
    assert bot.calls[1][1][0].caption is None, "the caption belongs to the first group"


async def test_a_row_written_before_kind_existed_keeps_its_old_routing() -> None:
    """The requested format was all the old rows had — documents would be a regression."""
    for quality, method in (("video", "send_video"), ("audio", "send_audio")):
        bot = FakeBot()

        assert await delivery.send_cached_file(bot, 1, _row(quality=quality))  # type: ignore[arg-type]

        assert bot.methods == [method], quality


async def test_anything_unrecognised_is_a_document() -> None:
    bot = FakeBot()

    assert await delivery.send_cached_file(bot, 1, _row(kind="", quality="best"))  # type: ignore[arg-type]

    assert bot.methods == ["send_document"]


# ---------------------------------------------------------------------------
# When Telegram says no
# ---------------------------------------------------------------------------


async def test_a_type_mismatch_falls_back_to_a_document() -> None:
    bot = FakeBot(failing=("send_photo",))

    assert await delivery.send_cached_file(bot, 1, _row(kind="photo"))  # type: ignore[arg-type]

    assert bot.methods == ["send_document"]


async def test_a_dead_file_id_tells_the_caller_to_download_again() -> None:
    bot = FakeBot(failing=("send_video", "send_document"))

    assert not await delivery.send_cached_file(bot, 1, _row(kind="video"))  # type: ignore[arg-type]

    assert bot.methods == []


async def test_a_row_with_no_id_is_dropped_without_a_call() -> None:
    bot = FakeBot()

    assert not await delivery.send_cached_file(bot, 1, _row(kind="photo", telegram_file_id=""))  # type: ignore[arg-type]

    assert bot.calls == []
