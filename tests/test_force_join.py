"""Force-join: regular users join the configured channels/groups first.

The gate is optional (empty ``FORCE_JOIN_TARGETS`` = disabled, zero overhead),
private-chat only, bypassed by premium/VIP and ADMIN_IDS, and fails open:
anything the check cannot prove — Telegram trouble, a dead cache, a slow
network — lets the user through while paging the admins once per target.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
import redis.exceptions as redis_errors
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from test_user_menu import _message

from core.catalog import MESSAGES
from core.config import Settings
from core.i18n import t
from handlers import user as user_module
from services import subscription as subscription_module
from services.subscription import (
    ForceJoinService,
    build_force_join,
    parse_force_join_targets,
)

USER_ID = 4242
ADMIN_ID = 777001
TARGET_AT = "@OurChannel"
TARGET_ID = "-100123456789"
RAW = f"{TARGET_AT}, {TARGET_ID}|https://t.me/+inviteHash|VIP Lounge"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _member(status: str, *, is_member: bool = False) -> Any:
    return SimpleNamespace(status=status, is_member=is_member)


class _FakeBot:
    """get_chat_member from a table; send_message recorded for admin pages."""

    def __init__(self, membership: dict[Any, Any] | None = None, bot_id: int = 999) -> None:
        self.membership = membership or {}
        self.chat_member_calls: list[tuple[Any, int]] = []
        self.sent: list[tuple[int, str]] = []
        self.calls: list[Any] = []
        self.id = bot_id

    async def get_chat_member(self, chat: Any, user_id: int) -> Any:
        self.chat_member_calls.append((chat, user_id))
        verdict = self.membership.get(
            (chat, user_id), self.membership.get(chat, "left")
        )
        if isinstance(verdict, BaseException):
            raise verdict
        if isinstance(verdict, tuple):
            return _member(verdict[0], is_member=verdict[1])
        return _member(verdict)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any:
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def __call__(self, method: Any) -> Any:
        """Serve real aiogram objects bound with ``.as_(bot)``."""
        self.calls.append(method)
        return SimpleNamespace(message_id=1)


class _StrictFakeRedis:
    """Accepts ``EX`` exactly like redis-py 8.1.0 (int/timedelta/digit-str)."""

    def __init__(self, *, down: bool = False, returns_bytes: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.ex_args: list[Any] = []
        self.calls: list[tuple[str, str]] = []
        self.down = down
        self.returns_bytes = returns_bytes

    async def get(self, key: str) -> Any:
        self.calls.append(("get", key))
        if self.down:
            raise ConnectionError("redis is down")
        value = self.values.get(key)
        if value is None:
            return None
        return value.encode("utf-8") if self.returns_bytes else value

    async def set(
        self, key: str, value: str, ex: Any = None, **kwargs: Any
    ) -> bool:
        self.calls.append(("set", key))
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

    async def ttl(self, key: str) -> int:
        return 60 if key in self.values else -2


class _FakePool:
    """bot_state through fetchval/execute, the way database.get/set_state use it."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    async def fetchval(self, query: str, key: str) -> str | None:
        return self.state.get(key)

    async def execute(self, query: str, key: str, value: str) -> str:
        self.state[key] = value
        return "INSERT 0 1"


def _user(
    user_id: int = USER_ID, *, premium: bool = False, admin: bool = False
) -> Any:
    return {
        "telegram_id": ADMIN_ID if admin else user_id,
        "is_premium": premium,
        "premium_until": None,
    }


def _settings(
    monkeypatch: pytest.MonkeyPatch, **env: str
) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(subscription_module, "get_settings", lambda: settings)
    return settings


def _service(
    monkeypatch: pytest.MonkeyPatch,
    bot: _FakeBot,
    redis: Any = None,
    pool: Any = None,
    raw: str = RAW,
    admin_ids: tuple[int, ...] = (ADMIN_ID,),
    ttl: str = "300",
) -> ForceJoinService:
    settings = _settings(
        monkeypatch, FORCE_JOIN_TARGETS=raw, FORCE_JOIN_CACHE_TTL_S=ttl
    )
    service = build_force_join(
        settings,
        bot=cast(Bot, bot),
        redis=redis,
        pool=pool,
        admin_ids=list(admin_ids),
    )
    assert service is not None
    return service


# ---------------------------------------------------------------------------
# parsing and the disabled default
# ---------------------------------------------------------------------------


def test_valid_entries_parse_into_immutable_targets() -> None:
    targets, invalid = parse_force_join_targets(RAW)
    assert invalid == ()
    assert len(targets) == 2
    first, second = targets
    assert (first.chat, first.url) == (TARGET_AT, "https://t.me/OurChannel")
    assert (second.chat, second.title) == (-100123456789, "VIP Lounge")
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(first, "key", "mutated")


def test_invalid_entries_are_skipped_not_fatal() -> None:
    targets, invalid = parse_force_join_targets(
        "borked, @ab, @has space, 123|https://t.me/+x, "
        "-1001|not-a-link|Title, -1001|https://t.me/+x|, @Good_123"
    )
    assert [t.key for t in targets] == ["good_123"]
    assert len(invalid) == 6


def test_empty_means_disabled_and_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, FORCE_JOIN_TARGETS="")
    assert build_force_join(
        settings, bot=cast(Bot, _FakeBot()), redis=None, pool=None, admin_ids=[]
    ) is None


def test_invalid_entries_draw_one_startup_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(monkeypatch, FORCE_JOIN_TARGETS="nope, @ab, @FineOne")
    with caplog.at_level(logging.WARNING, logger=subscription_module.__name__):
        service = build_force_join(
            settings, bot=cast(Bot, _FakeBot()), redis=None, pool=None, admin_ids=[]
        )
    assert service is not None and [t.key for t in service.targets] == ["fineone"]
    warnings = [r for r in caplog.records if "FORCE_JOIN_TARGETS" in r.message]
    assert len(warnings) == 1 and "nope" in warnings[0].message


def test_all_invalid_means_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(monkeypatch, FORCE_JOIN_TARGETS="nope")
    with caplog.at_level(logging.WARNING, logger=subscription_module.__name__):
        assert (
            build_force_join(
                settings,
                bot=cast(Bot, _FakeBot()),
                redis=None,
                pool=None,
                admin_ids=[],
            )
            is None
        )


async def test_disabled_makes_no_telegram_or_redis_calls() -> None:
    bot = _FakeBot()
    redis = _StrictFakeRedis()
    service = ForceJoinService(
        bot=cast(Bot, bot),
        redis=redis,
        pool=None,
        admin_ids=[],
        targets=(),
        cache_ttl_s=300,
    )
    assert not service.enabled
    assert await service.missing(_user(), USER_ID) == ()
    assert await service.recheck(USER_ID) == ()
    assert bot.chat_member_calls == [] and redis.calls == []


# ---------------------------------------------------------------------------
# the membership verdicts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["member", "administrator", "creator"])
async def test_plain_membership_passes_and_is_cached(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    bot = _FakeBot({TARGET_AT: status, -100123456789: status})
    redis = _StrictFakeRedis()
    service = _service(monkeypatch, bot, redis)

    assert await service.missing(_user(), USER_ID) == ()

    assert redis.ex_args and all(isinstance(ex, int) for ex in redis.ex_args)
    assert all(ex == 300 for ex in redis.ex_args)
    assert len(bot.chat_member_calls) == 2


async def test_a_second_check_inside_the_ttl_makes_no_telegram_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot({TARGET_AT: "member", -100123456789: "member"})
    redis = _StrictFakeRedis()
    service = _service(monkeypatch, bot, redis)

    assert await service.missing(_user(), USER_ID) == ()
    assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.chat_member_calls) == 2, "the second check is all cache"


async def test_a_non_member_is_blocked_and_negatives_are_never_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot({TARGET_AT: "left", -100123456789: "member"})
    redis = _StrictFakeRedis()
    service = _service(monkeypatch, bot, redis)

    missing = await service.missing(_user(), USER_ID)

    assert [t.key for t in missing] == ["ourchannel"]
    assert len(redis.values) == 1, "only the pass is remembered"
    assert await service.missing(_user(), USER_ID) == missing
    assert len(bot.chat_member_calls) == 3, "the negative is re-checked live"


@pytest.mark.parametrize(
    ("verdict", "passes"),
    [
        (("restricted", True), True),
        (("restricted", False), False),
        ("left", False),
        ("kicked", False),
    ],
)
async def test_restricted_counts_only_while_still_a_member(
    monkeypatch: pytest.MonkeyPatch, verdict: Any, passes: bool
) -> None:
    bot = _FakeBot({TARGET_AT: verdict, -100123456789: "member"})
    service = _service(monkeypatch, bot, None)

    missing = await service.missing(_user(), USER_ID)

    assert (missing == ()) is passes


async def test_admin_and_premium_bypass_without_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot()
    redis = _StrictFakeRedis()
    monkeypatch.setenv("ADMIN_IDS", str(ADMIN_ID))
    service = _service(monkeypatch, bot, redis)

    assert await service.missing(_user(admin=True), ADMIN_ID) == ()
    assert await service.missing(_user(premium=True), USER_ID) == ()
    assert bot.chat_member_calls == [] and redis.calls == []


# ---------------------------------------------------------------------------
# fail-open: telegram trouble, floods, timeouts, dead cache
# ---------------------------------------------------------------------------


def _troubled_bot(
    monkeypatch: pytest.MonkeyPatch, trouble: Any
) -> tuple[_FakeBot, _FakePool, ForceJoinService]:
    bot = _FakeBot({TARGET_AT: trouble, -100123456789: "member"})
    pool = _FakePool()
    service = _service(monkeypatch, bot, None, pool)
    return bot, pool, service


async def test_a_telegram_error_fails_open_with_one_warning_and_one_page(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot, pool, service = _troubled_bot(
        monkeypatch,
        TelegramBadRequest(method=None, message="chat not found"),  # type: ignore[arg-type]
    )
    with caplog.at_level(logging.WARNING, logger=subscription_module.__name__):
        assert await service.missing(_user(), USER_ID) == ()
    assert len([r for r in caplog.records if "ourchannel" in r.message]) == 1
    assert len(bot.sent) == 1 and bot.sent[0][0] == ADMIN_ID

    with caplog.at_level(logging.WARNING, logger=subscription_module.__name__):
        caplog.clear()
        assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.sent) == 1, "the 6h throttle holds the second page"
    assert pool.state, "the notice stamp survives a restart"


async def test_retry_after_fails_open_without_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trouble = TelegramRetryAfter(method=None, message="flood", retry_after=30)  # type: ignore[arg-type]
    bot, _, service = _troubled_bot(monkeypatch, trouble)

    async def _no_sleep(delay: float) -> None:
        raise AssertionError("the intake path never sleeps out a flood")

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.sent) == 1


async def test_a_timeout_fails_open(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot, _, service = _troubled_bot(monkeypatch, asyncio.TimeoutError())
    with caplog.at_level(logging.WARNING, logger=subscription_module.__name__):
        assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.sent) == 1


async def test_a_dead_cache_still_asks_telegram_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot({TARGET_AT: "member", -100123456789: "member"})
    service = _service(monkeypatch, bot, _StrictFakeRedis(down=True))

    assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.chat_member_calls) == 2


async def test_a_stalled_check_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    bot = _FakeBot()
    bot.get_chat_member = _hang  # type: ignore[method-assign]
    monkeypatch.setattr(subscription_module, "FORCE_JOIN_TOTAL_TIMEOUT_S", 0.05)
    service = _service(monkeypatch, bot, None)

    assert await service.missing(_user(), USER_ID) == ()


# ---------------------------------------------------------------------------
# the verify button: always fresh
# ---------------------------------------------------------------------------


async def test_verify_ignores_a_stale_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot({TARGET_AT: "member", -100123456789: "member"})
    redis = _StrictFakeRedis()
    service = _service(monkeypatch, bot, redis)
    assert await service.missing(_user(), USER_ID) == ()

    bot.membership[TARGET_AT] = "left"  # left after the pass was cached
    assert [t.key for t in await service.recheck(USER_ID)] == ["ourchannel"]
    assert len(bot.chat_member_calls) == 4, "recheck never reads the cache"


async def test_bytes_redis_clients_read_passes_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot({TARGET_AT: "member", -100123456789: "member"})
    redis = _StrictFakeRedis(returns_bytes=True)
    service = _service(monkeypatch, bot, redis)

    assert await service.missing(_user(), USER_ID) == ()
    assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.chat_member_calls) == 2


# ---------------------------------------------------------------------------
# startup validation
# ---------------------------------------------------------------------------


async def test_startup_validation_passes_a_joined_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot({TARGET_AT: "administrator", -100123456789: "member"})
    service = _service(monkeypatch, bot, None, _FakePool())

    assert await service.validate_bot_access() == ()
    assert bot.sent == []


async def test_startup_validation_names_what_the_bot_cannot_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _FakeBot({TARGET_AT: "left", -100123456789: "member"})
    service = _service(monkeypatch, bot, None, _FakePool())

    assert await service.validate_bot_access() == ("ourchannel",)
    assert len(bot.sent) == 1


async def test_startup_validation_never_raises_or_blocks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("telegram is gone")

    async def _hang(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3600)

    for get_member in (_boom, _hang):
        bot = _FakeBot()
        bot.get_chat_member = get_member  # type: ignore[method-assign]
        monkeypatch.setattr(subscription_module, "FORCE_JOIN_CHECK_TIMEOUT_S", 0.05)
        monkeypatch.setattr(subscription_module, "FORCE_JOIN_TOTAL_TIMEOUT_S", 0.1)
        service = _service(monkeypatch, bot, None, None)
        with caplog.at_level(logging.WARNING, logger=subscription_module.__name__):
            assert await service.validate_bot_access() == ()


# ---------------------------------------------------------------------------
# handler wiring: where the gate stands, and where it never does
# ---------------------------------------------------------------------------


def _gated_callbacks() -> set[str]:
    names: set[str] = set()
    for router in (user_module.router, user_module.force_join_router):
        for collection in (router.message.handlers, router.callback_query.handlers):
            for handler in collection:
                callback = handler.callback
                if "force_join" in inspect.signature(callback).parameters:
                    names.add(callback.__name__)
    return names


def test_the_gate_stands_on_exactly_five_handlers() -> None:
    # ``cmd_start`` joined in Session B: a ``/start dl_<digest>`` deep link
    # from inline mode runs the normal intake, so the gate must apply to that
    # intake — while plain ``/start`` itself stays ungated (pinned below).
    assert _gated_callbacks() == {
        "on_text_with_url",
        "cmd_download",
        "cmd_start",
        "on_menu_platform",
        "on_force_join_verify",
    }


async def test_plain_start_never_touches_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cmd_start`` carries ``force_join`` only to forward it into a deep-link
    intake. A payload-less ``/start`` — the onboarding path — must never ask
    Telegram about membership, with or without a configured gate."""
    from aiogram.filters import CommandObject
    from test_user_menu import RecordingBot
    from test_user_menu import _message as _user_message

    service_bot = _FakeBot()
    service = _service(monkeypatch, service_bot, _StrictFakeRedis())
    bot = RecordingBot()
    message = _user_message("/start", cast(Bot, bot))
    await user_module.cmd_start(
        message,
        _user(),
        command=CommandObject(prefix="/", command="start", args=""),
        lang="en",
        force_join=service,
    )
    assert service_bot.chat_member_calls == []


@pytest.mark.parametrize(
    "name",
    [
        "cmd_status",
        "cmd_profile",
        "on_menu_profile",
        "cmd_premium",
        "on_menu_premium",
        "on_menu_language",
        "on_language_chosen",
        "cmd_language",
        "on_menu_home",
        "on_menu_download",
        "on_menu_support",
    ],
)
def test_bypassed_paths_take_no_force_join_parameter(name: str) -> None:
    assert name not in _gated_callbacks(), f"{name} must never see the gate"
    assert hasattr(user_module, name), f"{name} still exists"


async def test_group_chats_are_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot()
    service = _service(monkeypatch, bot, _StrictFakeRedis())
    message = _message("https://example.com/x", cast(Bot, bot), chat_type="group")

    blocked = await user_module._enforce_force_join(
        message, _user(), "en", force_join=service
    )

    assert blocked is False
    assert bot.chat_member_calls == []


async def test_the_join_wall_lists_targets_with_url_and_verify_buttons() -> None:
    bot = _FakeBot()
    message = _message("https://example.com/x", cast(Bot, bot))
    targets = parse_force_join_targets(RAW)[0]

    await user_module._send_force_join(message, targets, "en")

    sent = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
    assert len(sent) == 1
    rows = sent[0].reply_markup.inline_keyboard
    flat = [b for row in rows for b in row]
    urls = [b.url for b in flat if b.url]
    assert "https://t.me/OurChannel" in urls and "https://t.me/+inviteHash" in urls
    verify = [b for b in flat if b.callback_data == "force_join:verify"]
    assert len(verify) == 1
    assert sent[0].text is not None and "VIP Lounge" in sent[0].text


# ---------------------------------------------------------------------------
# copy: bilingual, in sync, popup-short
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(k for k in MESSAGES if k.startswith("force_join.")))
def test_force_join_copy_is_bilingual_with_matching_placeholders(key: str) -> None:
    from core.texts import validate_text

    for lang in ("en", "fa"):
        assert MESSAGES[key][lang], f"{key} is missing {lang}"
        assert validate_text(key, MESSAGES[key][lang]) is None
    assert set(_placeholders(MESSAGES[key]["en"])) == set(
        _placeholders(MESSAGES[key]["fa"])
    )


def _placeholders(template: str) -> list[str]:
    import re

    return re.findall(r"\{(\w+)", template)


@pytest.mark.parametrize("lang", ["en", "fa"])
def test_popups_stay_within_telegrams_200_characters(lang: str) -> None:
    assert len(t("force_join.still_missing", lang)) <= 200
    assert len(t("force_join.verify_button", lang)) <= 200


@pytest.mark.parametrize("lang", ["en", "fa"])
def test_the_wall_renders_in_both_languages(lang: str) -> None:
    targets = parse_force_join_targets(RAW)[0]
    text = user_module._force_join_text(targets, lang)
    assert "VIP Lounge" in text and "force_join" not in text


# ---------------------------------------------------------------------------
# maintenance first tick
# ---------------------------------------------------------------------------


def _stub_maintenance(monkeypatch: pytest.MonkeyPatch, stop: asyncio.Event) -> None:
    from services import worker as worker_module

    async def _zero(*args: Any, **kwargs: Any) -> int:
        stop.set()
        return 0

    async def _nothing(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(worker_module.database, "expire_premiums", _zero)
    monkeypatch.setattr(worker_module.database, "prune_block_events", _nothing)
    monkeypatch.setattr(worker_module.database, "prune_helper_events", _nothing)
    monkeypatch.setattr(worker_module.database, "prune_stale_cache", _zero)
    monkeypatch.setattr(worker_module, "_sweep_download_dir", lambda *a, **k: (0, 0, 0))


async def test_the_first_maintenance_tick_validates_and_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import worker as worker_module

    stop = asyncio.Event()
    bot = _FakeBot({TARGET_AT: "left", -100123456789: "member"})
    _settings(monkeypatch, FORCE_JOIN_TARGETS=RAW)
    monkeypatch.setattr(
        worker_module, "get_settings", subscription_module.get_settings
    )
    _stub_maintenance(monkeypatch, stop)

    await asyncio.wait_for(
        worker_module.run_maintenance(stop, object(), cast(Bot, bot), (ADMIN_ID,)),
        timeout=10,
    )

    assert stop.is_set()
    assert len(bot.sent) == 1, "the absent bot pages once from the first tick"


async def test_a_disabled_deployment_validates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import worker as worker_module

    stop = asyncio.Event()
    bot = _FakeBot()
    _settings(monkeypatch, FORCE_JOIN_TARGETS="")
    monkeypatch.setattr(
        worker_module, "get_settings", subscription_module.get_settings
    )
    _stub_maintenance(monkeypatch, stop)

    await asyncio.wait_for(
        worker_module.run_maintenance(stop, object(), cast(Bot, bot), (ADMIN_ID,)),
        timeout=10,
    )

    assert bot.chat_member_calls == [] and bot.sent == []


# ---------------------------------------------------------------------------
# real Redis (the Linux gate points FORCE_JOIN_TEST_REDIS_URL at it)
# ---------------------------------------------------------------------------


async def _real_client() -> Any:
    import redis.asyncio as aioredis

    url = os.getenv("FORCE_JOIN_TEST_REDIS_URL", "")
    if not url:
        pytest.skip("set FORCE_JOIN_TEST_REDIS_URL to a disposable Redis")
    client = aioredis.from_url(url, decode_responses=True, socket_timeout=2.0)
    await client.ping()
    return client


async def test_cache_write_against_real_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await _real_client()
    try:
        bot = _FakeBot({TARGET_AT: "member", -100123456789: "member"})
        service = _service(monkeypatch, bot, client, None)
        assert await service.missing(_user(), USER_ID) == ()

        for target in service.targets:
            key = f"fj:ok:{target.key}:{USER_ID}"
            assert await client.exists(key) == 1
            ttl = await client.ttl(key)
            assert isinstance(ttl, int) and 1 <= ttl <= 300

        before = len(bot.chat_member_calls)
        assert await service.missing(_user(), USER_ID) == ()
        assert len(bot.chat_member_calls) == before, "cache hit, no Telegram call"
    finally:
        for target in parse_force_join_targets(RAW)[0]:
            try:
                await client.delete(f"fj:ok:{target.key}:{USER_ID}")
            except Exception:
                pass
        try:
            await client.aclose()
        except Exception:
            pass


async def test_negative_results_write_nothing_to_real_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await _real_client()
    try:
        bot = _FakeBot({TARGET_AT: "left", -100123456789: "kicked"})
        service = _service(monkeypatch, bot, client, None)
        missing = await service.missing(_user(), USER_ID)
        assert len(missing) == 2

        for target in service.targets:
            assert await client.exists(f"fj:ok:{target.key}:{USER_ID}") == 0
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


async def test_stale_bot_state_does_not_block_a_fresh_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _FakePool()
    pool.state["force_join:notice:ourchannel"] = (
        datetime.now(timezone.utc) - timedelta(hours=7)
    ).isoformat()
    trouble = TelegramBadRequest(method=None, message="chat not found")  # type: ignore[arg-type]
    bot = _FakeBot({TARGET_AT: trouble, -100123456789: "member"})
    service = _service(monkeypatch, bot, None, pool)

    assert await service.missing(_user(), USER_ID) == ()
    assert len(bot.sent) == 1, "a 7h-old stamp is due again"
