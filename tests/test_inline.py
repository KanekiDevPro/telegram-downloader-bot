"""Inline mode: ``@Bot <link>`` serves the already-downloaded, or a way in.

The inline path performs NO network I/O and NO DNS — only structural parsing
and a cache lookup. Anything it cannot prove (unknown user, exhausted quota,
unverified membership, no cache row, a link that would need redirect
resolution) becomes the deep-link button, never an error and never a wait.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
import redis.exceptions as redis_errors
from aiogram import Bot
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineQuery, Message, User

from core.catalog import MESSAGES
from core.i18n import t
from handlers import inline as inline_module
from handlers import user as user_module

USER_ID = 4242
ADMIN_ID = 777001
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
UNRESOLVABLE_URL = "https://no-such-host-example-xyz.invalid/watch?v=1"
SHORT_URL = "https://youtu.be/dQw4w9WgXcQ"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _StrictFakeRedis:
    """Accepts ``EX`` exactly like redis-py 8.1.0 (int/timedelta/digit-str)."""

    def __init__(self, *, down: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.ex_args: list[Any] = []
        self.expiry_args: list[Any] = []
        self.down = down

    async def get(self, key: str) -> str | None:
        if self.down:
            raise ConnectionError("redis is down")
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: Any = None, **kwargs: Any) -> bool:
        if self.down:
            raise ConnectionError("redis is down")
        import datetime as _dt

        if isinstance(ex, _dt.timedelta):
            ex = int(ex.total_seconds())
        elif isinstance(ex, bool) or not isinstance(ex, int):
            if not (isinstance(ex, str) and ex.isdigit()):
                raise redis_errors.DataError("ex must be datetime.timedelta or int")
            ex = int(ex)
        self.ex_args.append(ex)
        self.values[key] = value
        return True

    async def incr(self, key: str) -> int:
        if self.down:
            raise ConnectionError("redis is down")
        current = int(self.values.get(key, "0")) + 1
        self.values[key] = str(current)
        return current

    async def expire(self, key: str, seconds: Any) -> bool:
        if self.down:
            raise ConnectionError("redis is down")
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise redis_errors.DataError("expire must take an int")
        self.expiry_args.append(seconds)
        return True


class _FakePool:
    """Users, daily usage and cache rows keyed off the query text."""

    def __init__(
        self,
        *,
        users: dict[int, Any] | None = None,
        usage: dict[int, Any] | None = None,
        rows: list[Any] | None = None,
    ) -> None:
        self.users = users or {}
        self.usage = usage or {}
        self.rows = rows or []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "daily_downloads" in query:
            return self.usage.get(args[0])
        if "FROM users" in query:
            return self.users.get(args[0])
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "smart_cache" in query:
            return list(self.rows)
        return []


class _FakeBot:
    def __init__(self) -> None:
        self.answers: list[dict[str, Any]] = []
        self.sent: list[str] = []

    async def answer_inline_query(self, inline_query_id: str, **kwargs: Any) -> bool:
        self.answers.append({"id": inline_query_id, **kwargs})
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append(text)
        return SimpleNamespace(message_id=1)

    async def __call__(self, method: Any) -> Any:
        """Serve real aiogram objects bound with ``.as_(bot)``."""
        if type(method).__name__ == "SendMessage":
            self.sent.append(method.text or "")
            from aiogram.types import Chat, Message

            return Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=1, type=cast(Any, "private")),
                text=method.text or "",
            )
        raise AssertionError(f"unexpected method: {type(method).__name__}")


class _ForceJoinStub:
    def __init__(self, *, enabled: bool, passes: bool = True) -> None:
        self.enabled = enabled
        self._passes = passes
        self.checked: list[int] = []

    async def cached_pass(self, user_id: int) -> bool:
        self.checked.append(user_id)
        return self._passes


def _user(**overrides: Any) -> dict[str, Any]:
    row = {
        "telegram_id": USER_ID,
        "username": "ali",
        "is_premium": False,
        "premium_until": None,
        "language": "en",
        "is_new": False,
    }
    row.update(overrides)
    return row


def _row(**overrides: Any) -> dict[str, Any]:
    row = {
        "url_hash": "h",
        "original_url": URL,
        "platform": "youtube",
        "telegram_file_id": "file-id-1",
        "quality": "720p",
        "kind": "video",
        "created_at": datetime.now(timezone.utc),
    }
    row.update(overrides)
    return row


def _query(text: str, user_id: int = USER_ID) -> InlineQuery:
    return InlineQuery(
        id="qid-1",
        from_user=User(id=user_id, is_bot=False, first_name="u"),
        query=text,
        offset="",
    )


async def _ask(
    text: str,
    *,
    pool: _FakePool,
    redis: Any = None,
    force_join: Any = None,
    bot: _FakeBot | None = None,
    user_id: int = USER_ID,
) -> tuple[_FakeBot, dict[str, Any]]:
    bot = bot or _FakeBot()
    await inline_module.on_inline_query(
        _query(text, user_id),
        bot=cast(Bot, bot),
        pool=cast(Any, pool),
        redis=redis,
        force_join=force_join,
    )
    assert len(bot.answers) == 1, "every query gets exactly one answer"
    return bot, bot.answers[0]


# ---------------------------------------------------------------------------
# empty and non-link queries: silence, not errors
# ---------------------------------------------------------------------------


async def test_an_empty_query_answers_empty_with_no_button() -> None:
    bot, answer = await _ask("", pool=_FakePool(users={USER_ID: _user()}))

    assert answer["results"] == []
    assert answer.get("button") is None


async def test_a_non_link_query_answers_empty_with_no_button() -> None:
    bot, answer = await _ask("hello world", pool=_FakePool(users={USER_ID: _user()}))

    assert answer["results"] == []
    assert answer.get("button") is None


async def test_a_private_host_query_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`127.0.0.1` has no cache row (intake refuses such links), so it becomes
    the deep-link button — and resolving must never even be attempted."""

    def _no_dns(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the inline path performs no DNS")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    bot, answer = await _ask(
        "http://127.0.0.1/secret/x.mp4",
        pool=_FakePool(users={USER_ID: _user()}),
        redis=_StrictFakeRedis(),
    )

    assert answer["results"] == []
    assert answer.get("button") is not None


async def test_a_servable_host_needs_no_dns_to_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even the happy path resolves nothing: the row is keyed by the same
    canonical form the cache uses, no DNS required."""

    def _no_dns(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the inline path performs no DNS")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    pool = _FakePool(users={USER_ID: _user()}, rows=[_row()])
    bot, answer = await _ask(URL, pool=pool)

    assert len(answer["results"]) == 1


# ---------------------------------------------------------------------------
# cached rows become the matching result types, honestly captioned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "file_id", "result_type"),
    [
        ("video", "vid-1", "video"),
        ("audio", "aud-1", "audio"),
        ("photo", "pic-1", "photo"),
        ("file", "doc-1", "document"),
    ],
)
async def test_each_kind_maps_to_its_result_type(
    kind: str, file_id: str, result_type: str
) -> None:
    pool = _FakePool(
        users={USER_ID: _user()},
        rows=[_row(kind=kind, telegram_file_id=file_id, quality="best")],
    )
    bot, answer = await _ask(URL, pool=pool)

    assert len(answer["results"]) == 1
    result = answer["results"][0]
    assert result.type == result_type
    assert file_id in result.model_dump().values()
    assert result.caption, "a result without a caption is a dead end of trust"


async def test_captions_match_the_replay_caption() -> None:
    from services.delivery import replay_caption

    row = _row()
    pool = _FakePool(users={USER_ID: _user()}, rows=[row])
    bot, answer = await _ask(URL, pool=pool)

    assert answer["results"][0].caption == replay_caption(row, "en")


async def test_a_gallery_row_serves_its_first_file() -> None:
    import json

    pool = _FakePool(
        users={USER_ID: _user()},
        rows=[_row(kind="photo_group", telegram_file_id=json.dumps(["p1", "p2"]))],
    )
    bot, answer = await _ask(URL, pool=pool)

    assert len(answer["results"]) == 1
    assert answer["results"][0].photo_file_id == "p1"


async def test_results_are_capped_at_ten_with_short_deterministic_ids() -> None:
    rows = [_row(quality=f"{i}p", telegram_file_id=f"f-{i}") for i in range(15)]
    pool = _FakePool(users={USER_ID: _user()}, rows=rows)
    bot, answer = await _ask(URL, pool=pool)

    assert len(answer["results"]) == 10
    ids = [r.id for r in answer["results"]]
    assert all(len(i.encode("utf-8")) <= 64 for i in ids)
    assert len(set(ids)) == 10


async def test_best_quality_comes_first() -> None:
    rows = [
        _row(quality="360p", telegram_file_id="low"),
        _row(quality="1080p", telegram_file_id="high"),
        _row(quality="720p", telegram_file_id="mid"),
    ]
    pool = _FakePool(users={USER_ID: _user()}, rows=rows)
    bot, answer = await _ask(URL, pool=pool)

    assert answer["results"][0].video_file_id == "high"


async def test_answers_are_personal_with_a_small_cache_time() -> None:
    pool = _FakePool(users={USER_ID: _user()}, rows=[_row()])
    bot, answer = await _ask(URL, pool=pool)

    assert answer["is_personal"] is True
    assert 1 <= answer["cache_time"] <= 60


# ---------------------------------------------------------------------------
# gates: unknown users, quota, force-join
# ---------------------------------------------------------------------------


async def test_an_unknown_user_gets_only_the_start_button() -> None:
    bot, answer = await _ask(
        URL, pool=_FakePool(users={}), redis=_StrictFakeRedis()
    )

    assert answer["results"] == []
    button = answer.get("button")
    assert button is not None
    assert button.start_parameter in (None, ""), "no payload for strangers"


async def test_an_exhausted_quota_gets_the_deep_link_button() -> None:
    pool = _FakePool(
        users={USER_ID: _user()},
        usage={USER_ID: {"daily_downloads": 10, "last_download_date": inline_module._today()}},
        rows=[_row()],
    )
    bot = _FakeBot()
    await inline_module.on_inline_query(
        _query(URL),
        bot=cast(Bot, bot),
        pool=cast(Any, pool),
        redis=_StrictFakeRedis(),
        force_join=None,
    )

    assert bot.answers[0]["results"] == []
    button = bot.answers[0].get("button")
    assert button is not None and button.start_parameter.startswith("dl_")


async def test_force_join_without_a_cached_pass_gets_the_deep_link_button() -> None:
    pool = _FakePool(users={USER_ID: _user()}, rows=[_row()])
    fj = _ForceJoinStub(enabled=True, passes=False)
    bot, answer = await _ask(URL, pool=pool, redis=_StrictFakeRedis(), force_join=fj)

    assert answer["results"] == []
    assert fj.checked == [USER_ID]
    assert answer.get("button") is not None


async def test_force_join_with_a_cached_pass_serves() -> None:
    pool = _FakePool(users={USER_ID: _user()}, rows=[_row()])
    fj = _ForceJoinStub(enabled=True, passes=True)
    bot, answer = await _ask(URL, pool=pool, redis=_StrictFakeRedis(), force_join=fj)

    assert len(answer["results"]) == 1


async def test_premium_users_are_measured_against_the_premium_ceiling() -> None:
    """59 downloads sinks a regular user (limit 10) but not a VIP (limit 60)."""
    usage = {USER_ID: {"daily_downloads": 59, "last_download_date": inline_module._today()}}
    regular = _FakePool(users={USER_ID: _user()}, usage=usage, rows=[_row()])
    _, regular_answer = await _ask(URL, pool=regular)
    assert regular_answer["results"] == []

    vip = _FakePool(users={USER_ID: _user(is_premium=True)}, usage=usage, rows=[_row()])
    _, vip_answer = await _ask(URL, pool=vip)
    assert len(vip_answer["results"]) == 1


# ---------------------------------------------------------------------------
# tokens: int EX, idempotent, rate-limited, Redis-down fallback
# ---------------------------------------------------------------------------


async def test_a_cache_miss_stores_one_token_with_an_int_expiry() -> None:
    redis = _StrictFakeRedis()
    pool = _FakePool(users={USER_ID: _user()}, rows=[])
    bot, answer = await _ask(URL, pool=pool, redis=redis)

    assert answer["results"] == []
    button = answer.get("button")
    assert button is not None and button.start_parameter.startswith("dl_")
    assert redis.ex_args and all(isinstance(ex, int) for ex in redis.ex_args)
    assert all(ex == 3600 for ex in redis.ex_args)
    digest = button.start_parameter[3:]
    assert len(digest) == 16
    assert redis.values.get(f"dl:{digest}") == inline_module._canonical(URL)


async def test_the_token_is_idempotent_per_url() -> None:
    redis = _StrictFakeRedis()
    pool = _FakePool(users={USER_ID: _user()}, rows=[])
    _, first = await _ask(URL, pool=pool, redis=redis)
    _, second = await _ask(URL, pool=pool, redis=redis)

    assert first["button"].start_parameter == second["button"].start_parameter
    assert len(redis.values) == 2, "one token key plus one rate-limit key"


async def test_short_links_that_need_resolution_get_a_button_without_lookup() -> None:
    """`youtu.be` may redirect — the inline path must not resolve it, so it
    never even asks the cache: straight to the deep link."""
    class _NoCachePool(_FakePool):
        async def fetch(self, query: str, *args: Any) -> list[Any]:
            raise AssertionError("short links skip the cache lookup")

    pool = _NoCachePool(users={USER_ID: _user()}, rows=[_row()])
    bot, answer = await _ask(SHORT_URL, pool=pool, redis=_StrictFakeRedis())

    assert answer["results"] == []
    assert answer.get("button") is not None


async def test_redis_down_degrades_to_a_payload_less_button() -> None:
    pool = _FakePool(users={USER_ID: _user()}, rows=[])
    bot, answer = await _ask(URL, pool=pool, redis=_StrictFakeRedis(down=True))

    assert answer["results"] == []
    button = answer.get("button")
    assert button is not None
    assert button.start_parameter in (None, "")


async def test_token_creation_is_rate_limited_per_user() -> None:
    redis = _StrictFakeRedis()
    redis.values["dl:rl:4242"] = "30"
    pool = _FakePool(users={USER_ID: _user()}, rows=[])
    bot, answer = await _ask(URL, pool=pool, redis=redis)

    button = answer.get("button")
    assert button is not None
    assert button.start_parameter in (None, ""), "limited users get plain start"
    assert all(isinstance(s, int) for s in redis.expiry_args)


# ---------------------------------------------------------------------------
# /start dl_<digest>
# ---------------------------------------------------------------------------


def _message(text: str, bot: _FakeBot) -> Message:
    from aiogram.types import Chat

    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=USER_ID, type=cast(Any, "private")),
        from_user=User(id=USER_ID, is_bot=False, first_name="u"),
        text=text,
    ).as_(cast(Bot, bot))


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


def _command(args: str) -> CommandObject:
    return CommandObject(prefix="/", command="start", args=args)


async def test_start_with_a_valid_token_runs_the_normal_intake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.utils import canonical_url, sha256_hex

    digest = sha256_hex(canonical_url(URL))[:16]
    redis = _StrictFakeRedis()
    redis.values[f"dl:{digest}"] = canonical_url(URL)
    bot = _FakeBot()
    called: list[str] = []

    async def _fake_flow(
        message: Message, state: FSMContext, user: Any, url: str, lang: str, **kwargs: Any
    ) -> None:
        called.append(url)

    monkeypatch.setattr(user_module, "_queue_url_flow", _fake_flow)
    await user_module.cmd_start(
        _message(f"/start dl_{digest}", bot),
        _user(),
        command=_command(f"dl_{digest}"),
        state=_state(),
        bot=cast(Bot, bot),
        pool=cast(Any, _FakePool()),
        queue=cast(Any, object()),
        redis=redis,
        lang="en",
    )

    assert called == [canonical_url(URL)]


async def test_start_with_an_expired_token_asks_for_the_link_again() -> None:
    bot = _FakeBot()
    await user_module.cmd_start(
        _message("/start dl_deadbeefdeadbeef", bot),
        _user(),
        command=_command("dl_deadbeefdeadbeef"),
        state=_state(),
        bot=cast(Bot, bot),
        pool=cast(Any, _FakePool()),
        queue=cast(Any, object()),
        redis=_StrictFakeRedis(),
        lang="en",
    )

    assert bot.sent and "link" in bot.sent[0].lower()


@pytest.mark.parametrize("args", ["dl_zzz", "dl_123", "dl_", "dl_DEADBEEFDEADBEEF!", "hello"])
async def test_start_with_a_malformed_payload_ignores_it(args: str) -> None:
    bot = _FakeBot()
    await user_module.cmd_start(
        _message(f"/start {args}", bot),
        _user(),
        command=_command(args),
        state=_state(),
        bot=cast(Bot, bot),
        pool=cast(Any, _FakePool()),
        queue=cast(Any, object()),
        redis=_StrictFakeRedis(),
        lang="en",
    )

    assert bot.sent and "Hi" in bot.sent[0], "plain /start welcome, untouched"


async def test_start_without_redis_treats_tokens_as_expired() -> None:
    bot = _FakeBot()
    await user_module.cmd_start(
        _message("/start dl_deadbeefdeadbeef", bot),
        _user(),
        command=_command("dl_deadbeefdeadbeef"),
        state=_state(),
        bot=cast(Bot, bot),
        pool=cast(Any, _FakePool()),
        queue=cast(Any, object()),
        redis=None,
        lang="en",
    )

    assert bot.sent and "link" in bot.sent[0].lower()


# ---------------------------------------------------------------------------
# copy: bilingual, valid, short popups-free buttons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(k for k in MESSAGES if k.startswith("inline.")))
def test_inline_copy_is_bilingual_with_matching_placeholders(key: str) -> None:
    from core.texts import validate_text

    for lang in ("en", "fa"):
        assert MESSAGES[key][lang], f"{key} is missing {lang}"
        assert validate_text(key, MESSAGES[key][lang]) is None


def test_button_texts_fit_telegrams_limits() -> None:
    for lang in ("en", "fa"):
        assert len(t("inline.open_bot", lang)) <= 64
        assert len(t("inline.open_bot_plain", lang)) <= 64


# ---------------------------------------------------------------------------
# real Redis (the Linux gate points INLINE_TEST_REDIS_URL at it)
# ---------------------------------------------------------------------------


async def _real_client() -> Any:
    import redis.asyncio as aioredis

    url = os.getenv("INLINE_TEST_REDIS_URL", "") or os.getenv(
        "FORCE_JOIN_TEST_REDIS_URL", ""
    )
    if not url:
        pytest.skip("set INLINE_TEST_REDIS_URL to a disposable Redis")
    client = aioredis.from_url(url, decode_responses=True, socket_timeout=2.0)
    await client.ping()
    return client


async def test_token_write_against_real_redis() -> None:
    client = await _real_client()
    try:
        pool = _FakePool(users={USER_ID: _user()}, rows=[])
        bot = _FakeBot()
        await inline_module.on_inline_query(
            _query(URL),
            bot=cast(Bot, bot),
            pool=cast(Any, pool),
            redis=client,
            force_join=None,
        )
        button = bot.answers[0].get("button")
        assert button is not None and button.start_parameter.startswith("dl_")
        key = f"dl:{button.start_parameter[3:]}"
        assert await client.exists(key) == 1
        ttl = await client.ttl(key)
        assert isinstance(ttl, int) and 1 <= ttl <= 3600
    finally:
        try:
            for key in await client.keys("dl:*"):
                await client.delete(key)
        except Exception:
            pass
        try:
            await client.aclose()
        except Exception:
            pass
