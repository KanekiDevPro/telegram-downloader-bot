"""Song lookup under Instagram/TikTok videos (session B2).

Every behavior change here has a test: the metadata table (real songs in,
captions and original sounds out), button visibility (default config shows
nothing without a detected song), replay parity, the Redis mapping with its
int EX, expiry/cooldown/negative-cache answers, the concurrency bound, the
breaker, popup lengths, and the tap handlers reaching the normal intake.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from types import SimpleNamespace
from typing import Any, cast

import pytest
import redis.exceptions as redis_errors
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageCaption
from aiogram.types import CallbackQuery, Chat, Message, User, Video

import handlers.user as user_module
from core.i18n import t
from services.extractor import SearchHit
from services.song_id import (
    SONG_COOLDOWN_MAX,
    SONG_COOLDOWN_WINDOW_S,
    SONG_MAPPING_TTL_S,
    SONG_NEGATIVE_TTL_S,
    SongLookupService,
    identify_from_metadata,
    parse_providers,
    search_query,
)


class StrictFakeRedis:
    """Mirrors redis-py's EX rule (int/timedelta/digit-str) plus INCR/EXPIRE."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ex_args: list[Any] = []
        self.expire_args: list[Any] = []

    async def set(
        self, key: str, value: str, ex: Any = None, **kwargs: Any
    ) -> bool:
        if ex is not None:
            if isinstance(ex, datetime.timedelta):
                ex = int(ex.total_seconds())
            elif isinstance(ex, bool) or not isinstance(ex, int):
                if not (isinstance(ex, str) and ex.isdigit()):
                    raise redis_errors.DataError("ex must be datetime.timedelta or int")
                ex = int(ex)
            self.ex_args.append(ex)
        self.values[key] = value
        return True

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def incr(self, key: str) -> int:
        current = int(self.values.get(key, "0")) + 1
        self.values[key] = str(current)
        return current

    async def expire(self, key: str, seconds: Any) -> bool:
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise redis_errors.DataError("expire must be int")
        self.expire_args.append(seconds)
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


def make_service(redis: Any = None, **kwargs: Any) -> SongLookupService:
    params: dict[str, Any] = {"redis": redis if redis is not None else StrictFakeRedis()}
    params.update(kwargs)
    return SongLookupService(**params)


def video_message(bot: Any, caption: str = "cap") -> Message:
    return Message(
        message_id=5,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=Chat(id=7, type="private"),
        from_user=User(id=7, is_bot=False, first_name="user"),
        video=Video(
            file_id="vid", file_unique_id="uniq", width=320, height=640, duration=10
        ),
        caption=caption,
    ).as_(cast(Bot, bot))


class RecordingBot:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        return True

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    @property
    def captions(self) -> list[Any]:
        return [call for call in self.calls if isinstance(call, EditMessageCaption)]


def make_tap(bot: RecordingBot, data: str) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=7, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=video_message(bot),
    ).as_(cast(Bot, bot))


USER: dict[str, Any] = {"telegram_id": 7}


# ----------------------------------------------------------------------
# Metadata provider table
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (
            {"track": "Blinding Lights", "artist": "The Weeknd"},
            ("The Weeknd", "Blinding Lights"),
        ),
        (
            {"title": "The Weeknd - Blinding Lights"},
            ("The Weeknd", "Blinding Lights"),
        ),
        (
            {"title": "\u266a Dua Lipa \u2013 Levitating"},
            ("Dua Lipa", "Levitating"),
        ),
        (
            {"title": "BTS \u2014 Dynamite"},
            ("BTS", "Dynamite"),
        ),
        (
            {"music": {"title": "Levitating", "author": "Dua Lipa"}},
            ("Dua Lipa", "Levitating"),
        ),
        # Unicode names pass through untouched.
        (
            {"title": "\u062d\u0645\u06cc\u062f - \u0622\u0647\u0646\u06af \u062a\u0627\u0632\u0647"},
            ("\u062d\u0645\u06cc\u062f", "\u0622\u0647\u0646\u06af \u062a\u0627\u0632\u0647"),
        ),
        # No real song below this line.
        ({"title": "original sound", "uploader": "someone"}, None),
        ({"title": "Original Sound", "creator": "someone"}, None),
        ({"track": "", "artist": ""}, None),
        ({"track": "My Jam", "artist": ""}, None),
        ({"title": ""}, None),
        ({}, None),
        (None, None),
        # The creator's name alone is not a song.
        ({"title": "creator1", "uploader": "creator1"}, None),
        ({"title": "creator1 - creator1", "uploader": "creator1"}, None),
        ({"artist": "creator1", "uploader": "creator1"}, None),
        # Captions, hashtags and filenames are doubt, and doubt is None.
        ({"title": "My day at the beach - so much fun #summer"}, None),
        ({"title": "#fyp #viral"}, None),
        ({"title": "Artist-Title"}, None),
        ({"title": "A - B"}, None),
        ({"title": "x" * 200}, None),
        ({"title": "http://example.com/a - b"}, None),
        ({"title": "@someone - hello"}, None),
    ],
)
def test_metadata_table(info: Any, expected: Any) -> None:
    identity = identify_from_metadata(info)
    if expected is None:
        assert identity is None
    else:
        assert identity is not None
        assert (identity.artist, identity.title) == expected
        assert identity.source == "metadata"


def test_metadata_never_raises() -> None:
    assert identify_from_metadata(object()) is None
    assert identify_from_metadata({"track": ["odd", "shape"]}) is None


# ----------------------------------------------------------------------
# Provider parsing
# ----------------------------------------------------------------------


def test_parse_providers() -> None:
    assert parse_providers("metadata") == (("metadata",), ())
    assert parse_providers("metadata, shazamio") == (("metadata", "shazamio"), ())
    assert parse_providers("audd, metadata") == (("metadata",), ("audd",))
    assert parse_providers("") == ((), ())


def test_unknown_providers_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="services.song_id"):
        make_service(providers="audd, metadata, bogus")
    warnings = [
        record for record in caplog.records if "unknown song providers" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "audd" in warnings[0].getMessage() and "bogus" in warnings[0].getMessage()


def test_default_providers_metadata_only() -> None:
    assert make_service().providers == ("metadata",)


def test_search_query() -> None:
    assert search_query("  The Weeknd ", "Blinding  Lights ") == "The Weeknd - Blinding Lights"


# ----------------------------------------------------------------------
# Button visibility + replay parity
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_button_without_detected_song_default() -> None:
    songs = make_service()
    assert (
        await songs.button_for_delivery(
            platform="instagram", title="just a caption", url="https://instagram.com/p/x", lang="en"
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_button_off_platform() -> None:
    songs = make_service()
    assert (
        await songs.button_for_delivery(
            platform="youtube",
            title="Artist - Title",
            url="https://www.youtube.com/watch?v=x",
            lang="en",
        )
        is None
    )


@pytest.mark.asyncio
async def test_button_present_when_song_detected() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    markup = await songs.button_for_delivery(
        platform="tiktok",
        title="Dua Lipa - Levitating",
        url="https://www.tiktok.com/@u/video/1",
        lang="en",
    )
    assert markup is not None
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) == 1
    assert buttons[0].callback_data is not None
    assert buttons[0].callback_data.startswith("shz:")
    assert len(buttons[0].callback_data) == len("shz:") + 16
    # The mapping was written with an int EX of exactly 86400.
    assert redis.ex_args == [int(SONG_MAPPING_TTL_S)]
    assert all(isinstance(arg, int) for arg in redis.ex_args)
    stored = await songs.read_mapping(buttons[0].callback_data[len("shz:"):])
    assert stored is not None
    assert stored["artist"] == "Dua Lipa" and stored["title"] == "Levitating"


@pytest.mark.asyncio
async def test_same_button_state_on_cached_replay() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    url = "https://www.tiktok.com/@u/video/1"
    fresh = await songs.button_for_delivery(
        platform="tiktok", title="Dua Lipa - Levitating", url=url, lang="en"
    )
    row = {"kind": "video", "platform": "tiktok", "title": "Dua Lipa - Levitating"}
    replay = await songs.button_for_row(row, url, "en")
    assert fresh is not None and replay is not None
    fresh_data = fresh.inline_keyboard[0][0].callback_data
    replay_data = replay.inline_keyboard[0][0].callback_data
    assert fresh_data == replay_data
    assert await songs.button_for_row({**row, "kind": "photo"}, url, "en") is None


@pytest.mark.asyncio
async def test_no_button_when_redis_down() -> None:
    songs = SongLookupService(redis=None)
    assert (
        await songs.button_for_delivery(
            platform="instagram",
            title="Artist - Title",
            url="https://instagram.com/p/x",
            lang="en",
        )
        is None
    )


# ----------------------------------------------------------------------
# Cooldown + negative cache (int TTLs)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cooldown_budget() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    for _ in range(SONG_COOLDOWN_MAX):
        assert await songs.check_cooldown(7) is True
    assert await songs.check_cooldown(7) is False
    assert redis.expire_args == [int(SONG_COOLDOWN_WINDOW_S)]
    assert all(isinstance(arg, int) for arg in redis.expire_args)


@pytest.mark.asyncio
async def test_negative_cache_roundtrip() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    assert await songs.is_negative("a" * 16) is False
    await songs.remember_negative("a" * 16)
    assert await songs.is_negative("a" * 16) is True
    assert redis.ex_args == [int(SONG_NEGATIVE_TTL_S)]
    assert all(isinstance(arg, int) for arg in redis.ex_args)


def test_strict_fake_rejects_float_ex() -> None:
    async def run() -> None:
        with pytest.raises(redis_errors.DataError):
            await StrictFakeRedis().set("k", "v", ex=60.5)

    asyncio.run(run())


# ----------------------------------------------------------------------
# Concurrency bound + breaker
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency() -> None:
    songs = make_service(max_concurrency=2)
    first = await songs.slot().acquire()
    assert first is True
    second = await songs.slot().acquire()
    assert second is True
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(songs.slot().acquire(), timeout=0.05)
    songs.slot().release()
    songs.slot().release()


@pytest.mark.asyncio
async def test_breaker_opens_and_hides_button() -> None:
    now = [1000.0]
    songs = make_service(providers="metadata,shazamio", clock=lambda: now[0])
    assert songs.recognizer_available() is True
    assert songs.note_failure("shazamio") is False
    assert songs.note_failure("shazamio") is False
    assert songs.note_failure("shazamio") is True
    assert songs.is_healthy("shazamio") is False
    # While open, an unnamed song gets no button (rule 9).
    assert (
        await songs.button_for_delivery(
            platform="instagram", title="just a caption",
            url="https://instagram.com/p/x", lang="en",
        )
        is None
    )
    now[0] += 601.0
    assert songs.is_healthy("shazamio") is True
    songs.note_success("shazamio")
    assert songs.is_healthy("shazamio") is True


# ----------------------------------------------------------------------
# Strings: en/fa present, popups short
# ----------------------------------------------------------------------


@pytest.mark.parametrize("key", ["shz.expired", "shz.cooldown", "shz.no_match"])
@pytest.mark.parametrize("lang", ["en", "fa"])
def test_popups_short(key: str, lang: str) -> None:
    assert len(t(key, lang)) <= 200


def test_manual_fallback_popup_stays_short() -> None:
    for lang in ("en", "fa"):
        assert len(t("shz.send_manually", lang, candidate="x" * 120)) <= 200


# ----------------------------------------------------------------------
# Tap handlers
# ----------------------------------------------------------------------


class FakeExtractor:
    def __init__(self, hits: list[SearchHit] | None = None) -> None:
        self.hits = hits if hits is not None else []
        self.queries: list[str] = []

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        self.queries.append(query)
        return self.hits


@pytest.mark.asyncio
async def test_tap_shows_identity_and_candidate() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    bot = RecordingBot()
    markup = await songs.button_for_delivery(
        platform="instagram",
        title="Dua Lipa - Levitating",
        url="https://instagram.com/p/x",
        lang="en",
    )
    assert markup is not None
    data = markup.inline_keyboard[0][0].callback_data or ""
    extractor = FakeExtractor(
        [SearchHit(url="https://www.youtube.com/watch?v=abc", title="Dua Lipa - Levitating", duration_s=200)]
    )
    cb = make_tap(bot, data)
    await user_module.on_song_tap(
        cb, USER, pool=None, queue=None, bot=None, lang="en",
        song_lookup=songs, extractor=cast(Any, extractor), force_join=None,
    )
    assert extractor.queries == ["Dua Lipa - Levitating"]
    assert len(bot.captions) == 1
    caption = bot.captions[0]
    assert "Dua Lipa" in (caption.caption or "")
    buttons = caption.reply_markup.inline_keyboard[0] if caption.reply_markup else []
    assert len(buttons) == 1
    assert (buttons[0].callback_data or "").startswith("shz:go:")


@pytest.mark.asyncio
async def test_tap_expired_mapping_popup() -> None:
    songs = make_service()
    bot = RecordingBot()
    cb = make_tap(bot, "shz:" + "0" * 16)
    await user_module.on_song_tap(
        cb, USER, pool=None, queue=None, bot=None, lang="en",
        song_lookup=songs, extractor=cast(Any, FakeExtractor()), force_join=None,
    )
    assert len(bot.answers) == 2  # the quick ack, then the popup
    assert (bot.answers[-1].text or "") == t("shz.expired", "en")
    assert bot.captions == []


@pytest.mark.asyncio
async def test_tap_no_match_popup_and_negative_cache() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    bot = RecordingBot()
    markup = await songs.button_for_delivery(
        platform="instagram",
        title="Dua Lipa - Levitating",
        url="https://instagram.com/p/x",
        lang="en",
    )
    assert markup is not None
    data = markup.inline_keyboard[0][0].callback_data or ""
    cb = make_tap(bot, data)
    await user_module.on_song_tap(
        cb, USER, pool=None, queue=None, bot=None, lang="en",
        song_lookup=songs, extractor=cast(Any, FakeExtractor([])), force_join=None,
    )
    assert len(bot.answers) == 2
    assert (bot.answers[-1].text or "") == t("shz.no_match", "en")
    assert await songs.is_negative(data[len("shz:"):]) is True


@pytest.mark.asyncio
async def test_tap_cooldown_popup() -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    bot = RecordingBot()
    markup = await songs.button_for_delivery(
        platform="instagram",
        title="Dua Lipa - Levitating",
        url="https://instagram.com/p/x",
        lang="en",
    )
    assert markup is not None
    data = markup.inline_keyboard[0][0].callback_data or ""
    for _ in range(SONG_COOLDOWN_MAX):
        assert await songs.check_cooldown(7) is True
    cb = make_tap(bot, data)
    await user_module.on_song_tap(
        cb, USER, pool=None, queue=None, bot=None, lang="en",
        song_lookup=songs, extractor=cast(Any, FakeExtractor()), force_join=None,
    )
    assert len(bot.answers) == 2
    assert (bot.answers[-1].text or "") == t("shz.cooldown", "en")


@pytest.mark.asyncio
async def test_confirm_reaches_normal_intake(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = StrictFakeRedis()
    songs = make_service(redis)
    calls: list[dict[str, Any]] = []

    async def fake_submit(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, "kwargs": kwargs})

    monkeypatch.setattr(user_module, "_submit", fake_submit)
    go = await songs.store_mapping(
        artist="Dua Lipa",
        title="Levitating",
        url="https://www.youtube.com/watch?v=abc",
        candidate="Dua Lipa - Levitating",
    )
    assert go is not None
    bot = RecordingBot()
    cb = make_tap(bot, f"shz:go:{go}")
    await user_module.on_song_tap(
        cb, USER, pool=object(), queue=object(), bot=object(), lang="en",
        song_lookup=songs, extractor=None, force_join=None,
    )
    assert len(calls) == 1
    args = calls[0]["args"]
    # (bot, message, pool, queue, user, url, "audio", "mp3.best", lang)
    assert args[5] == "https://www.youtube.com/watch?v=abc"
    assert args[6] == "audio"
    assert args[7] == "mp3.best"
    assert calls[0]["kwargs"]["title"] == "Dua Lipa - Levitating"


@pytest.mark.asyncio
async def test_confirm_expired_popup() -> None:
    songs = make_service()
    bot = RecordingBot()
    cb = make_tap(bot, "shz:go:" + "0" * 16)
    await user_module.on_song_tap(
        cb, USER, pool=object(), queue=object(), bot=object(), lang="fa",
        song_lookup=songs, extractor=None, force_join=None,
    )
    assert len(bot.answers) == 2
    assert (bot.answers[-1].text or "") == t("shz.expired", "fa")


@pytest.mark.asyncio
async def test_popup_absorbs_flood() -> None:
    from aiogram.exceptions import TelegramRetryAfter

    class FloodTap:
        async def answer(self, *args: Any, **kwargs: Any) -> None:
            raise TelegramRetryAfter(method=cast(Any, None), message="flood", retry_after=3)

    await user_module._song_popup(cast(Any, FloodTap()), "hi")


# ----------------------------------------------------------------------
# Real Redis (the Linux gate points SHZ_TEST_REDIS_URL at it)
# ----------------------------------------------------------------------


def _real_redis_url() -> str | None:
    return os.getenv("SHZ_TEST_REDIS_URL", "") or None


@pytest.mark.asyncio
async def test_real_redis_mapping_lifecycle() -> None:
    url = _real_redis_url()
    if not url:
        pytest.skip("set SHZ_TEST_REDIS_URL to a disposable Redis")
    import redis.asyncio as aioredis

    client = aioredis.from_url(url, decode_responses=True)
    try:
        songs = SongLookupService(redis=client)
        digest = await songs.store_mapping(
            artist="Dua Lipa", title="Levitating", url="https://instagram.com/p/x"
        )
        assert digest is not None
        stored = await songs.read_mapping(digest)
        assert stored is not None and stored["artist"] == "Dua Lipa"
        ttl = await client.ttl(f"shz:{digest}")
        assert isinstance(ttl, int) and 86000 < ttl <= 86400
        assert await songs.check_cooldown(4242) is True
        await songs.remember_negative(digest)
        assert await songs.is_negative(digest) is True
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# C3: degraded spells warn once and page once, then re-arm on recovery
# ---------------------------------------------------------------------------


class _DownRedis(StrictFakeRedis):
    """A Redis that answers reads but refuses every mapping write."""

    async def set(
        self, key: str, value: str, ex: Any = None, **kwargs: Any
    ) -> bool:
        if key.startswith("shz:") and not (
            key.startswith("shz:rl:") or key.startswith("shz:neg:")
        ):
            raise ConnectionError("redis is down")
        return await super().set(key, value, ex=ex, **kwargs)


class _NoticeBot:
    """Records operator pages the way the force-join fakes do."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)


class _NoticePool:
    """bot_state through fetchval/execute, the way database.get/set_state use it."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    async def fetchval(self, query: str, key: str) -> str | None:
        return self.state.get(key)

    async def execute(self, query: str, key: str, value: str) -> str:
        self.state[key] = value
        return "INSERT 0 1"


def _degraded_service(
    redis: Any, bot: Any = None, pool: Any = None
) -> SongLookupService:
    return SongLookupService(
        redis=redis, pool=pool, bot=bot, admin_ids=[777001],
    )


async def test_sustained_mapping_failures_warn_once_and_page_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, pool = _NoticeBot(), _NoticePool()
    songs = _degraded_service(_DownRedis(), bot=bot, pool=pool)
    with caplog.at_level(logging.WARNING, logger="services.song_id"):
        for _ in range(3):
            assert await songs.store_mapping(
                artist="A", title="T", url="https://www.instagram.com/p/x/"
            ) is None
    warnings = [r for r in caplog.records if "degraded" in r.message]
    assert len(warnings) == 1, "one warning per degraded spell, not per tap"
    assert "ConnectionError" in warnings[0].message
    assert len(bot.sent) == 1 and bot.sent[0][0] == 777001
    assert pool.state, "the notice stamp survives a restart"


async def test_recovery_re_arms_the_spell_while_the_throttle_holds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, pool = _NoticeBot(), _NoticePool()
    songs = _degraded_service(_DownRedis(), bot=bot, pool=pool)
    with caplog.at_level(logging.INFO, logger="services.song_id"):
        assert await songs.store_mapping(
            artist="A", title="T", url="https://www.instagram.com/p/x/"
        ) is None
        songs._redis = StrictFakeRedis()
        digest = await songs.store_mapping(
            artist="A", title="T", url="https://www.instagram.com/p/x/"
        )
        assert digest is not None, "the button works again after recovery"
        songs._redis = _DownRedis()
        assert await songs.store_mapping(
            artist="A", title="T", url="https://www.instagram.com/p/x/"
        ) is None
    warnings = [r for r in caplog.records if "degraded" in r.message]
    assert len(warnings) == 2, "recovery re-arms: the next spell warns again"
    assert any("watching again" in r.message for r in caplog.records)
    assert len(bot.sent) == 1, "the 6h throttle holds the second page"


async def test_healthy_mapping_path_is_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    songs = _degraded_service(StrictFakeRedis())
    with caplog.at_level(logging.DEBUG, logger="services.song_id"):
        for _ in range(2):
            assert await songs.store_mapping(
                artist="A", title="T", url="https://www.instagram.com/p/x/"
            ) is not None
    assert [r for r in caplog.records if "degraded" in r.message] == []
    assert [r for r in caplog.records if "watching again" in r.message] == []


async def test_no_redis_warns_once_without_paging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, pool = _NoticeBot(), _NoticePool()
    songs = _degraded_service(None, bot=bot, pool=pool)
    with caplog.at_level(logging.WARNING, logger="services.song_id"):
        for _ in range(2):
            assert await songs.store_mapping(
                artist="A", title="T", url="https://www.instagram.com/p/x/"
            ) is None
    warnings = [r for r in caplog.records if "degraded" in r.message]
    assert len(warnings) == 1, "a static no-Redis setup warns once per process"
    assert bot.sent == [], "a static setup is not an outage: no page"
