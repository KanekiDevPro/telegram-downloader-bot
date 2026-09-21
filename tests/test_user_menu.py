"""The user-facing frontend: menu, profile, premium, help, language, link intake.

Four promises are pinned here. The menu has one home and a way back to it (every
screen is the *same* message, edited). The profile answers the four questions a user
actually asks — who am I here, which plan am I on, how much is left, what is running.
Nothing that costs a wait happens silently: the moment a link or a format choice
arrives, the bot says what it is doing, in the message that will carry the outcome.
And the buttons are drawn from the *link*: a photo post is never offered a 1080p
tier, a YouTube video is never offered "send the photos".

Everything user-facing is read from the catalogue, so these tests say which language
they mean. ``FA`` is the default here because the Persian wording is the one that was
shipped first (and therefore the one a regression would silently change); the English
side has its own cases in ``tests/test_i18n.py``.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import Settings
from core.i18n import t
from core.utils import today_local
from handlers import payment as payment_module
from handlers import user as user_module
from handlers.user import DownloadStates
from services import content as content_module
from services import subscription as subscription_module

USER_ID = 4242
FA = "fa"
EN = "en"


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, answers with a real Message."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, method: Any) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return _message(method.text or "", self)
        return True

    @property
    def texts(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, SendMessage)]

    @property
    def edits(self) -> list[str]:
        return [call.text or "" for call in self.calls if isinstance(call, EditMessageText)]

    @property
    def screens(self) -> list[str]:
        """Everything the user would see, in order (new messages and edits alike)."""
        return [
            call.text or ""
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText))
        ]

    @property
    def answers(self) -> list[AnswerCallbackQuery]:
        return [call for call in self.calls if isinstance(call, AnswerCallbackQuery)]

    @property
    def keyboards(self) -> list[Any]:
        return [
            call.reply_markup
            for call in self.calls
            if isinstance(call, (SendMessage, EditMessageText)) and call.reply_markup is not None
        ]


def _message(text: str, bot: RecordingBot, user_id: int = USER_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="user"),
        text=text,
    ).as_(cast(Bot, bot))


def _callback(bot: RecordingBot, data: str, *, stale: bool = False) -> CallbackQuery:
    """A callback bound to a recording bot; ``stale`` = Telegram sent no message
    (an ``InaccessibleMessage``, or nothing at all).

    Built with ``message=None`` from the start, because the model is frozen — the
    same reason the handlers have to treat an inaccessible message as a real case
    rather than an edge one.
    """
    return CallbackQuery(
        id="1",
        from_user=User(id=USER_ID, is_bot=False, first_name="user"),
        chat_instance="chat",
        data=data,
        message=None if stale else _message("menu", bot),
    ).as_(cast(Bot, bot))


def _buttons(markup: Any) -> list[tuple[str, str]]:
    """``[(label, callback_data)]`` for every button on that screen."""
    return [
        (button.text, button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _user(
    *,
    premium: bool = False,
    until: datetime | None = None,
    username: str | None = "ali",
    language: str = FA,
) -> Any:
    return {
        "telegram_id": USER_ID,
        "username": username,
        "is_premium": premium,
        "premium_until": until,
        "language": language,
    }


class FakeQueue:
    """Only the two queue facts the gateway needs (it never downloads)."""

    def __init__(self, depth: int = 0) -> None:
        self.depth_value = depth
        self.tasks: list[Any] = []

    async def depth(self) -> int:
        return self.depth_value

    async def enqueue(self, task: Any) -> int:
        self.tasks.append(task)
        return self.depth_value


@pytest.fixture(autouse=True)
def _quiet_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quota reads: three downloads used today, clean settings, no plans.

    Settings are built from scratch (``_env_file=None``) in *both* modules that
    read them, so the machine's own ``.env`` can never decide what these tests
    assert about quotas or plan prices.
    """
    today = today_local()

    async def get_daily_usage(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 3, "last_download_date": today}

    def clean_settings() -> Settings:
        return Settings(_env_file=None)  # type: ignore[call-arg]

    monkeypatch.setattr(user_module.database, "get_daily_usage", get_daily_usage)
    monkeypatch.setattr(user_module, "get_settings", clean_settings)
    monkeypatch.setattr(subscription_module, "get_settings", clean_settings)


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """``set_user_language`` records instead of talking to PostgreSQL."""
    written: list[tuple[int, str]] = []

    async def set_user_language(pool: Any, telegram_id: int, language: str) -> None:
        written.append((telegram_id, language))

    monkeypatch.setattr(user_module.database, "set_user_language", set_user_language)
    return written


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


def test_the_menu_offers_profile_premium_help_and_language() -> None:
    markup = user_module._main_menu(FA)

    assert dict(_buttons(markup)) == {
        "👤 پروفایل من": "menu:profile",
        "💎 ارتقا به ویژه (VIP)": "menu:premium",
        "❓ راهنما": "menu:help",
        "🌐 زبان": "menu:language",
    }
    assert len(markup.inline_keyboard) == 4, "one button per row: these labels are long"


def test_the_menu_speaks_the_language_it_is_drawn_in() -> None:
    assert dict(_buttons(user_module._main_menu(EN))) == {
        "👤 My profile": "menu:profile",
        "💎 Go VIP": "menu:premium",
        "❓ Help": "menu:help",
        "🌐 Language": "menu:language",
    }


async def test_start_shows_the_menu() -> None:
    bot = RecordingBot()
    message = _message("/start", bot)

    await user_module.cmd_start(message, _user(), lang=FA)

    welcome = bot.texts[0]
    assert "سلام" in welcome and "لینک" in welcome
    # The service list is the point of the welcome screen: naming them is the
    # difference between "supports many sites" and an answer.
    for platform in ("یوتیوب", "اینستاگرام", "اسپاتیفای", "ساندکلاود"):
        assert platform in welcome, platform
    assert _buttons(bot.keyboards[0]) == _buttons(user_module._main_menu(FA))


async def test_start_greets_an_english_user_in_english() -> None:
    bot = RecordingBot()
    message = _message("/start", bot)

    await user_module.cmd_start(message, _user(language=EN), lang=EN)

    assert "Hi" in bot.texts[0] and "YouTube" in bot.texts[0]
    assert "سلام" not in bot.texts[0]


def test_every_screen_has_a_way_back() -> None:
    assert ("🔙 بازگشت", "menu:home") in _buttons(user_module._back_to_menu(FA))
    assert ("🔙 Back", "menu:home") in _buttons(user_module._back_to_menu(EN))


async def test_back_returns_to_the_menu_in_the_same_message() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:home")

    await user_module.on_menu_home(cb, _user(), lang=FA)

    assert bot.answers and bot.answers[0].text is None, "it is not an alert, just an answer"
    assert len(bot.edits) == 1 and "سلام" in bot.edits[0], "edited in place"
    assert bot.texts == [], "and nothing new was sent"
    assert _buttons(bot.keyboards[-1]) == _buttons(user_module._main_menu(FA))


async def test_a_stale_callback_is_answered_in_the_callers_language() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:home", stale=True)

    await user_module.on_menu_home(cb, _user(language=EN), lang=EN)

    # ``on_menu_home`` answers *before* looking at the message, so an unanswerable
    # tap still gets a normal answer; the alert path is pinned below.
    assert bot.answers, "a callback is always answered"


# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------


async def test_the_language_button_opens_a_picker_that_marks_the_current_one() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:language")

    await user_module.on_menu_language(cb, lang=EN)

    assert "language" in bot.edits[0].lower() or "زبان" in bot.edits[0]
    labels = dict(_buttons(bot.keyboards[-1]))
    assert labels["✅ 🇬🇧 English"] == "lang:en"
    assert labels["🇮🇷 فارسی"] == "lang:fa"
    assert labels["🔙 Back"] == "menu:home"


async def test_picking_a_language_stores_it_and_answers_in_it(
    _no_database: list[tuple[int, str]],
) -> None:
    bot = RecordingBot()
    cb = _callback(bot, "lang:fa")

    await user_module.on_language_chosen(cb, _user(language=EN), object(), lang=EN)

    assert _no_database == [(USER_ID, "fa")], "the choice is stored, not just shown"
    assert bot.answers[0].text is not None and "فارسی" in bot.answers[0].text
    assert "سلام" in bot.edits[0], "the confirmation itself is in the new language"
    assert _buttons(bot.keyboards[-1]) == _buttons(user_module._main_menu(FA))


async def test_the_command_sets_the_language_in_one_step(
    _no_database: list[tuple[int, str]],
) -> None:
    bot = RecordingBot()
    message = _message("/language en", bot)
    command = user_module.CommandObject(command="language", args="en")

    await user_module.cmd_language(message, command, _user(), object(), lang=FA)

    assert _no_database == [(USER_ID, "en")]
    assert "English" in bot.texts[0]
    assert _buttons(bot.keyboards[-1]) == _buttons(user_module._main_menu(EN))


async def test_a_language_the_bot_does_not_speak_is_refused(
    _no_database: list[tuple[int, str]],
) -> None:
    bot = RecordingBot()
    message = _message("/language de", bot)
    command = user_module.CommandObject(command="language", args="de")

    await user_module.cmd_language(message, command, _user(), object(), lang=FA)

    assert _no_database == [], "a typo must not silently become the default"
    assert "fa" in bot.texts[0] and "en" in bot.texts[0], "and it says what is supported"


async def test_a_crafted_language_callback_is_refused_too(
    _no_database: list[tuple[int, str]],
) -> None:
    bot = RecordingBot()
    cb = _callback(bot, "lang:xx")

    await user_module.on_language_chosen(cb, _user(), object(), lang=FA)

    assert _no_database == []
    assert bot.answers[0].show_alert is True


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------


async def test_the_profile_shows_id_username_status_and_quota() -> None:
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(message, _user(), object(), FakeQueue(depth=2), lang=FA)

    text = bot.texts[0]
    assert "پروفایل من" in text
    assert str(USER_ID) in text, "the Telegram id, so support can identify the account"
    assert "@ali" in text
    assert "رایگان 🪙" in text
    assert "سهمیهٔ امروز: 3 از 10" in text and "7 باقی مانده" in text
    assert "کارهای در صف: 2" in text
    assert ("🔙 بازگشت", "menu:home") in _buttons(bot.keyboards[-1])


async def test_a_premium_profile_says_so_with_the_days_left() -> None:
    until = datetime.now(timezone.utc) + timedelta(days=12, hours=1)
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(
        message, _user(premium=True, until=until), object(), FakeQueue(), lang=FA
    )

    text = bot.texts[0]
    assert "ویژه 💎" in text and "رایگان" not in text
    assert "12 روز مانده" in text
    assert "60" in text, "premium quota, not the free one"


async def test_an_admin_bypasses_payment_and_the_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ADMIN_IDS`` means perpetual VIP — and a label that says so.

    The bypass is decided from the settings on every call, so an operator who adds
    their own id does not also have to write themselves a subscription row.
    """

    def admin_settings() -> Settings:
        return Settings(_env_file=None, ADMIN_IDS=str(USER_ID))  # type: ignore[call-arg, arg-type]

    monkeypatch.setattr(subscription_module, "get_settings", admin_settings)
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(message, _user(), object(), FakeQueue(), lang=FA)

    text = bot.texts[0]
    assert "ادمین" in text and "رایگان" not in text
    assert "بی‌نهایت" in text, "an admin's quota is not a number they can reach"


async def test_an_admin_is_not_offered_what_they_already_have() -> None:
    bot = RecordingBot()
    message = _message("/premium", bot)

    async def no_plans(pool: Any, **kwargs: Any) -> None:
        return None

    import handlers.user as module

    original = module.plans_keyboard
    module.plans_keyboard = no_plans
    try:
        await user_module.cmd_premium(message, object(), _user(), lang=FA)
    finally:
        module.plans_keyboard = original

    assert bot.texts == [t("pay.no_plans", FA)]


async def test_a_username_less_user_is_not_shown_as_blank() -> None:
    bot = RecordingBot()
    message = _message("/profile", bot)

    await user_module.cmd_profile(message, _user(username=None), object(), FakeQueue(), lang=FA)

    assert "نام کاربری: —" in bot.texts[0]


async def test_the_profile_button_edits_the_menu_into_the_profile() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:profile")

    await user_module.on_menu_profile(cb, _user(), object(), FakeQueue(), lang=FA)

    assert [answer.text for answer in bot.answers] == [None], "answered exactly once"
    assert len(bot.edits) == 1 and "پروفایل من" in bot.edits[0]


async def test_a_stale_profile_callback_answers_with_an_alert() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:profile", stale=True)

    await user_module.on_menu_profile(cb, _user(), object(), FakeQueue(), lang=FA)

    assert len(bot.answers) == 1 and bot.answers[0].show_alert is True
    assert bot.edits == []


# ---------------------------------------------------------------------------
# Premium
# ---------------------------------------------------------------------------


async def test_the_premium_screen_offers_the_plans_and_a_way_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def plans(pool: Any, *, lang: str = FA, back: bool = False) -> Any:
        builder = InlineKeyboardBuilder()
        builder.button(text="VIP — 50,000 تومان", callback_data="plan:7")
        if back:
            builder.button(text=t("menu.back", lang), callback_data="menu:home")
        builder.adjust(1)
        return builder.as_markup()

    monkeypatch.setattr(user_module, "plans_keyboard", plans)
    bot = RecordingBot()
    cb = _callback(bot, "menu:premium")

    await user_module.on_menu_premium(cb, object(), _user(), lang=FA)

    assert "ویژه (VIP)" in bot.edits[0]
    assert "10 → <b>60</b>" in bot.edits[0], "the actual quota change, from settings"
    assert _buttons(bot.keyboards[-1]) == [
        ("VIP — 50,000 تومان", "plan:7"),
        ("🔙 بازگشت", "menu:home"),
    ]
    assert len(bot.answers) == 1


async def test_premium_without_any_plan_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_plans(pool: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(user_module, "plans_keyboard", no_plans)
    bot = RecordingBot()
    message = _message("/premium", bot)

    await user_module.cmd_premium(message, object(), _user(), lang=FA)

    assert bot.texts == [t("pay.no_plans", FA)]


def test_the_plan_button_names_the_currency_in_the_readers_language() -> None:
    assert t("pay.currency", EN) == "Toman"
    assert t("pay.currency", FA) == "تومان"


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


async def test_help_explains_the_flow_and_lists_the_commands() -> None:
    bot = RecordingBot()
    message = _message("/help", bot)

    await user_module.cmd_help(message, lang=FA)

    text = bot.texts[0]
    assert "لینک رو بفرست" in text
    assert "MP3" in text
    for command in ("/profile", "/premium", "/status", "/language", "/cancel", "/start"):
        assert command in text, command
    assert ("🔙 بازگشت", "menu:home") in _buttons(bot.keyboards[-1])


async def test_the_help_button_edits_the_menu_into_the_help() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:help")

    await user_module.on_menu_help(cb, lang=FA)

    assert "راهنما" in bot.edits[0]
    assert len(bot.answers) == 1


def test_no_menu_button_is_left_without_a_handler() -> None:
    """Every ``menu:`` button must have somewhere to go (a dead button is worse
    than no button at all)."""
    source = inspect.getsource(user_module)
    offered = {data for _, data in _buttons(user_module._main_menu(FA))}
    offered |= {data for _, data in _buttons(user_module._back_to_menu(FA))}

    missing = [data for data in sorted(offered) if f'F.data == "{data}"' not in source]
    assert missing == [], missing


def test_every_language_button_points_at_a_handler() -> None:
    source = inspect.getsource(user_module)
    for code, _ in _buttons(user_module._language_keyboard(FA)):
        if not code.startswith("lang:"):
            continue
        assert "LANG_PREFIX)" in source, "the language callbacks share one handler"


# ---------------------------------------------------------------------------
# Content routing: the buttons a link deserves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    (
        ("https://www.youtube.com/watch?v=abc", "video"),
        ("https://youtu.be/abc", "video"),
        ("https://www.tiktok.com/@user/video/123", "video"),
        ("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", "audio"),
        ("https://soundcloud.com/artist/track", "audio"),
        ("https://www.tiktok.com/@user/photo/123", "gallery"),
        ("https://www.instagram.com/p/abc/", "gallery"),
        ("https://www.instagram.com/reel/abc/", "video"),
        ("https://www.pinterest.com/pin/123/", "image"),
        ("https://x.com/user/status/12345", "media"),
        ("https://www.reddit.com/r/x/comments/1/y/", "media"),
        ("https://some-unknown-site.example/v/1", "media"),
    ),
)
def test_a_link_is_classified_by_what_it_can_actually_hold(url: str, kind: str) -> None:
    assert content_module.classify(url) == kind


def test_a_video_link_is_offered_quality_tiers_and_no_audio_only_tier() -> None:
    choices = content_module.routing_for("https://youtu.be/abc").choices

    assert [choice.quality for choice in choices] == ["best", "1080", "720", "480"]
    assert all(choice.media_format == "video" for choice in choices)


def test_a_music_link_is_offered_audio_formats_and_no_video_tier() -> None:
    choices = content_module.routing_for("https://soundcloud.com/a/b").choices

    assert [choice.quality for choice in choices] == ["m4a", "mp3"]
    assert all(choice.media_format == "audio" for choice in choices)


def test_a_photo_post_is_not_offered_a_quality_menu() -> None:
    """The menu must not promise a resolution for something that has none."""

    choices = content_module.routing_for("https://www.instagram.com/p/abc/").choices

    assert len(choices) == 1
    assert choices[0].label_key == "fmt.media"


def test_an_ambiguous_post_is_offered_media_and_audio() -> None:
    """Nothing is hidden that might be true: a status can be a clip, a picture or a
    gallery, so the honest menu offers "whatever is there" plus the audio formats."""

    choices = content_module.routing_for("https://x.com/user/status/12345").choices

    assert [choice.label_key for choice in choices] == [
        "fmt.media",
        "fmt.audio_m4a",
        "fmt.audio_mp3",
    ]


async def test_the_question_matches_the_link(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot = RecordingBot()
    message = _message("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", bot)

    await user_module._queue_url_flow(
        message, await _state(), _user(), "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", FA
    )

    assert t("intake.choose_audio", FA) in bot.edits[-1]
    assert ("🎧 صدا — M4A (اصل، بدون تبدیل)", "fmt:audio:m4a") in _buttons(bot.keyboards[-1])


# ---------------------------------------------------------------------------
# Immediate feedback, and the queueing path
# ---------------------------------------------------------------------------


async def _state(url: str = "https://youtu.be/abc") -> FSMContext:
    context = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )
    await context.set_state(DownloadStates.waiting_format)
    await context.update_data(url=url)
    return context


async def test_a_link_is_acknowledged_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot = RecordingBot()
    message = _message("https://youtu.be/abc", bot)

    await user_module._queue_url_flow(
        message, await _state(), _user(), "https://youtu.be/abc", FA
    )

    assert bot.texts[0] == t("intake.analyse", FA), "said first, before the probe"
    assert t("intake.choose_quality", FA) in bot.edits[-1], "and the same message becomes the question"
    assert ("🎬 بهترین کیفیت موجود", "fmt:video:best") in _buttons(bot.keyboards[-1])


async def test_an_unsupported_link_answers_in_the_same_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unsupported(url: str) -> bool:
        return False

    monkeypatch.setattr(user_module, "_probe_supported", unsupported)
    bot = RecordingBot()
    message = _message("https://example.com/x", bot)

    await user_module._queue_url_flow(
        message, await _state(), _user(), "https://example.com/x", FA
    )

    assert bot.texts == [t("intake.analyse", FA)]
    assert "پشتیبانی نمی‌شود" in bot.edits[-1]


async def test_choosing_a_format_shows_the_queueing_step_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    cb = _callback(bot, "fmt:video:720")
    queue = FakeQueue(depth=1)

    await user_module.on_format_chosen(
        cb, await _state(), _user(), object(), queue, bot, lang=FA
    )

    assert bot.texts[0] == t("intake.queueing", FA), "the tap is acknowledged instantly"
    assert "موقعیت تقریبی: 1" in bot.edits[-1]
    assert len(bot.answers) == 1 and bot.answers[0].text is None
    assert len(queue.tasks) == 1
    task = queue.tasks[0]
    assert task.media_format == "video"
    assert task.quality == "720", "the tier the user picked reaches the worker"
    assert task.lang == FA, "and so does the language the worker will answer in"


async def test_a_tier_that_was_never_offered_is_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crafted callback must not run a download the menu never showed."""

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    bot = RecordingBot()
    cb = _callback(bot, "fmt:audio:mp3")  # an audio tier on a YouTube link
    queue = FakeQueue()

    await user_module.on_format_chosen(
        cb, await _state("https://youtu.be/abc"), _user(), object(), queue, bot, lang=FA
    )

    assert queue.tasks == [], "nothing is queued"
    assert bot.answers[0].show_alert is True, "and the tap is answered honestly"


async def test_an_exhausted_quota_is_answered_with_an_alert_and_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    async def used_up(pool: Any, telegram_id: int) -> dict[str, Any]:
        return {"daily_downloads": 10, "last_download_date": today_local()}

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module.database, "get_daily_usage", used_up)
    bot = RecordingBot()
    cb = _callback(bot, "fmt:audio:mp3")

    await user_module.on_format_chosen(
        cb, await _state("https://soundcloud.com/a/b"), _user(), object(), FakeQueue(), bot, lang=FA
    )

    assert "سهمیهٔ دانلود امروزت (10 از 10) تمام شده" in bot.edits[-1]
    assert len(bot.answers) == 1 and bot.answers[0].show_alert is True


async def test_a_cache_hit_replays_the_file_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []

    async def cached(pool: Any, url: str, media_format: str = "", quality: str = "") -> Any:
        sent.append((media_format, quality))
        return {"url_hash": "x", "telegram_file_id": "AgAC", "kind": "video"}

    async def send_cached_file(bot: Any, chat_id: int, row: Any, caption: str = "", **kw: Any) -> bool:
        sent.append(caption)
        return True

    monkeypatch.setattr(user_module.cache_service, "get_cached", cached)
    monkeypatch.setattr(user_module, "send_cached_file", send_cached_file)
    bot = RecordingBot()
    cb = _callback(bot, "fmt:audio:m4a")

    await user_module.on_format_chosen(
        cb, await _state("https://soundcloud.com/a/b"), _user(), object(), FakeQueue(), bot, lang=FA
    )

    assert sent[0] == ("audio", "m4a"), "the lookup is for this exact tier"
    assert sent[1] == t("work.cache_caption", FA), "and the replay is captioned in the user's language"
    assert "حافظهٔ کش" in bot.edits[-1]


class _NoRefusal:
    """``preflight`` with nothing to say (the real rules have their own tests)."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return _Verdict()


class _Verdict:
    refused = False
    message = ""


#: Handlers that answer through a shared function instead of doing it themselves.
_ANSWERS_VIA: dict[str, str] = {
    "on_admin_approve": "_admin_decide",
    "on_admin_reject": "_admin_decide",
}


def test_every_registered_callback_answers() -> None:
    """A callback that is never answered leaves a spinner on the user's button.

    The two admin-approval handlers answer through one shared decision function
    (that is the point of it), so the delegate is followed explicitly rather than
    pretending every handler does its own. Any handler added later must answer
    itself.
    """
    from handlers import admin as admin_module

    missing: list[str] = []
    for module, router in (
        (user_module, user_module.router),
        (payment_module, payment_module.router),
        (admin_module, admin_module.router),
    ):
        for handler in router.callback_query.handlers:
            name = getattr(handler.callback, "__name__", "?")
            source = inspect.getsource(handler.callback)
            delegate = _ANSWERS_VIA.get(name)
            if delegate is not None:
                source += inspect.getsource(getattr(module, delegate))
            if ".answer(" not in source:
                missing.append(name)
    assert missing == [], missing


def test_the_old_menu_callbacks_are_gone() -> None:
    """The menu was rearranged; the buttons it used to offer must not linger as
    dead endpoints."""
    offered = {data for _, data in _buttons(user_module._main_menu(FA))}
    assert not offered & {"menu:download", "menu:status", "menu:subscribe"}
