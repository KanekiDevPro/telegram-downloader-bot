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
    language: str | None = FA,
    is_new: bool = False,
) -> Any:
    return {
        "telegram_id": USER_ID,
        "username": username,
        "is_premium": premium,
        "premium_until": until,
        "language": language,
        "is_new": is_new,
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


def _fake_queue(depth: int = 0) -> Any:
    """The stub, typed loosely: the handlers declare the real queue class, and this
    stands in for it (two methods, no Redis)."""
    return FakeQueue(depth=depth)


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


def test_the_home_screen_offers_the_three_hubs() -> None:
    markup = user_module._main_menu(FA)

    assert dict(_buttons(markup)) == {
        "⬇️ دانلود": "menu:download",
        "👤 پروفایل من": "menu:profile",
        "❓ راهنما": "menu:help",
    }
    # Download first and alone: it is why most people came. The account actions
    # (VIP, Language) moved under Profile — a home carrying every action is a
    # wall of buttons.
    assert [len(row) for row in markup.inline_keyboard] == [1, 2]


def test_the_menu_speaks_the_language_it_is_drawn_in() -> None:
    assert dict(_buttons(user_module._main_menu(EN))) == {
        "⬇️ Download": "menu:download",
        "👤 My profile": "menu:profile",
        "❓ Help": "menu:help",
    }


def test_the_menu_hides_the_vip_button_from_an_admin() -> None:
    """VIP is permanent for an admin, so the button could only lead to a screen
    explaining that they cannot buy it.

    The admin gets the panel there instead: `/admin` is a working command, but a
    command nobody can see is indistinguishable from a missing feature — which is
    exactly how the panel was reported as "not accessible" while nothing was broken.
    """
    markup = user_module._main_menu(FA, admin=True)

    assert "menu:premium" not in dict(_buttons(markup)).values()
    assert "menu:language" not in dict(_buttons(markup)).values(), "language lives under Profile"
    assert dict(_buttons(markup)) == {
        "⬇️ دانلود": "menu:download",
        "👤 پروفایل من": "menu:profile",
        "❓ راهنما": "menu:help",
        "🛠 پنل مدیریت": "menu:admin",
    }


def test_an_admins_profile_does_not_offer_the_store_either() -> None:
    """The store moved, it did not disappear — except where buying is impossible."""
    offered = dict(_buttons(user_module._profile_keyboard(FA, admin=True)))

    assert "menu:premium" not in offered.values()
    assert dict(_buttons(user_module._profile_keyboard(FA))) == {
        "🌐 زبان": "profile:language",
        "💎 ارتقا به ویژه (VIP)": "menu:premium",
        "⬅️ بازگشت": "menu:home",
    }


def test_only_an_admin_is_offered_the_panel() -> None:
    assert "menu:admin" not in dict(_buttons(user_module._main_menu(EN))).values()
    assert "menu:admin" not in dict(
        _buttons(user_module._main_menu(EN, support=True))
    ).values()


def test_the_support_button_appears_only_when_somebody_configured_it() -> None:
    assert "menu:support" not in dict(_buttons(user_module._main_menu(FA))).values()

    markup = user_module._main_menu(FA, support=True)

    assert dict(_buttons(markup))["💬 پشتیبانی"] == "menu:support"


@pytest.mark.parametrize(
    ("contact", "target"),
    (
        ("@helpdesk", "https://t.me/helpdesk"),
        ("helpdesk", "https://t.me/helpdesk"),
        ("https://t.me/somegroup", "https://t.me/somegroup"),
        ("https://example.com/support", "https://example.com/support"),
        # Anything else is shown as it is rather than turned into a link that goes
        # nowhere: an operator may legitimately write "email me at …".
        ("support@example.com", ""),
        ("", ""),
    ),
)
def test_a_support_contact_becomes_a_link_only_when_it_is_one(
    contact: str, target: str
) -> None:
    assert user_module.support_target(contact) == target


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


async def test_a_brand_new_user_is_asked_for_a_language_first() -> None:
    """Registration already guessed the language from the Telegram locale — a
    guess must not count as a choice. The first contact gets the picker anyway,
    said in both languages, because the reader's language is exactly what is
    unknown on this one screen."""
    bot = RecordingBot()
    message = _message("/start", bot)

    await user_module.cmd_start(message, _user(is_new=True), lang=FA)

    text = bot.texts[0]
    assert "برای شروع" in text and "To start" in text
    assert dict(_buttons(bot.keyboards[0])) == {
        "🇬🇧 English": "lang:en",
        "🇮🇷 فارسی": "lang:fa",
    }, "no current choice to mark — and no menu to fall back to"
    assert bot.edits == [] and len(bot.texts) == 1, "one message, nothing else"


async def test_a_returning_user_is_never_asked_again() -> None:
    bot = RecordingBot()
    message = _message("/start", bot)

    await user_module.cmd_start(message, _user(), lang=FA)

    assert t("language.first_time", FA) not in bot.texts
    assert all(
        not data.startswith("lang:") for _, data in _buttons(bot.keyboards[0])
    ), "the picker is reachable only through 'Change language'"


def test_a_record_that_carries_no_language_is_a_first_contact_too() -> None:
    """A row from before the language column — or a loose test double."""
    assert user_module._needs_language_screen({"telegram_id": 1}) is True
    assert user_module._needs_language_screen(_user(language=None)) is True
    assert user_module._needs_language_screen(_user()) is False


def test_every_screen_has_a_way_back() -> None:
    assert ("⬅️ بازگشت", "menu:home") in _buttons(user_module._back_to_menu(FA))
    assert ("⬅️ Back", "menu:home") in _buttons(user_module._back_to_menu(EN))
    # A help page goes back to the hub it came from, not two levels up.
    assert ("⬅️ Back", "menu:help") in _buttons(user_module._back_to_menu(EN, to="menu:help"))


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
    assert labels["⬅️ Back"] == "menu:home"


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


async def test_the_profile_opens_the_same_picker_with_its_own_way_back() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "profile:language")

    await user_module.on_profile_language(cb, _fresh_state(), lang=FA)

    assert "زبان" in bot.edits[0]
    labels = dict(_buttons(bot.keyboards[-1]))
    assert labels["✅ 🇮🇷 فارسی"] == "lang:fa", "the current one is marked"
    assert labels["🇬🇧 English"] == "lang:en"
    assert labels["⬅️ بازگشت"] == "menu:profile", "the picker hands back where it was opened"


async def test_a_language_chosen_from_the_profile_returns_to_the_profile(
    _no_database: list[tuple[int, str]],
) -> None:
    """The same message becomes the profile again — in the new language. No chain
    of new screens, and no dump at the Home with the user wondering where they
    were (the back-and-back journey stays exactly as short as it looks)."""
    bot = RecordingBot()
    state = _fresh_state()
    await state.update_data(lang_return="profile")

    await user_module.on_language_chosen(
        _callback(bot, "lang:en"),
        _user(),
        object(),
        lang=FA,
        state=state,
        queue=FakeQueue(),
    )

    assert _no_database == [(USER_ID, "en")]
    assert t("profile.title", EN) in bot.edits[0], "the profile, re-rendered in English"
    assert _buttons(bot.keyboards[-1]) == _buttons(user_module._profile_keyboard(EN))
    assert await state.get_data() == {}, "the way back is one-shot"


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
    assert "🇮🇷 فارسی" in text, "the language row: 'change it' needs an obvious home"
    # The screen owns its account actions: language and VIP moved here from Home.
    assert dict(_buttons(bot.keyboards[-1])) == {
        "🌐 زبان": "profile:language",
        "💎 ارتقا به ویژه (VIP)": "menu:premium",
        "⬅️ بازگشت": "menu:home",
    }


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
        ("⬅️ بازگشت", "menu:home"),
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


async def test_help_is_a_hub_of_short_pages() -> None:
    bot = RecordingBot()
    message = _message("/help", bot)

    await user_module.cmd_help(message, lang=FA)

    text = bot.texts[0]
    assert "راهنما" in text
    assert "لینک رو بفرست" not in text, "the wall of text is one tap away, not in the hub"
    assert dict(_buttons(bot.keyboards[-1])) == {
        "🔧 چطور کار می‌کند": "help:how",
        "🌐 سرویس‌های پشتیبانی‌شده": "help:platforms",
        "🧩 مشکلات رایج": "help:problems",
        "💬 پشتیبانی": "menu:support",
        "⬅️ بازگشت": "menu:home",
    }


async def test_the_how_it_works_page_explains_the_flow_and_lists_the_commands() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "help:how")

    await user_module.on_help_page(cb, lang=FA)

    text = bot.edits[0]
    assert "لینک رو بفرست" in text
    assert "MP3" in text
    for command in ("/profile", "/premium", "/status", "/language", "/cancel", "/start"):
        assert command in text, command
    assert len(bot.answers) == 1


async def test_a_help_page_goes_back_to_the_hub() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "help:platforms")

    await user_module.on_help_page(cb, lang=EN)

    assert "Supported platforms" in bot.edits[0]
    assert ("⬅️ Back", "menu:help") in _buttons(bot.keyboards[-1])
    assert bot.texts == [], "a page is the same message edited"


async def test_the_help_button_edits_the_menu_into_the_help() -> None:
    bot = RecordingBot()
    cb = _callback(bot, "menu:help")

    await user_module.on_menu_help(cb, lang=FA)

    assert "راهنما" in bot.edits[0]
    assert len(bot.answers) == 1


def test_no_menu_button_is_left_without_a_handler() -> None:
    """Every button a user can be shown must have somewhere to go (a dead button
    is worse than no button at all).

    ``menu:admin`` is answered by the *admin* router — keeping it out of the user
    module is deliberate separation, not a gap. The three help pages share one
    handler and every ``lang:`` tap another, so those are matched by pattern.
    """
    from handlers import admin as admin_module

    source = inspect.getsource(user_module) + inspect.getsource(admin_module)
    offered = {data for _, data in _buttons(user_module._main_menu(FA, admin=True, support=True))}
    offered |= {data for _, data in _buttons(user_module._back_to_menu(FA))}
    offered |= {data for _, data in _buttons(user_module._profile_keyboard(FA))}
    offered |= {data for _, data in _buttons(user_module._help_keyboard(FA))}
    offered |= {data for _, data in _buttons(user_module._language_keyboard(FA))}

    def has_handler(data: str) -> bool:
        if f'F.data == "{data}"' in source:
            return True
        if data.startswith("help:"):
            return "F.data.in_(set(_HELP_PAGES))" in source and data in user_module._HELP_PAGES
        if data.startswith("lang:"):
            return "F.data.startswith(LANG_PREFIX)" in source
        return False

    missing = [data for data in sorted(offered) if not has_handler(data)]
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
        message,
        await _state(),
        _user(),
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=_fake_queue(),
    )

    assert t("intake.choose_audio", FA) in bot.edits[-1]
    assert ("🎧 صدا — M4A (اصل، بدون تبدیل)", "fmt:audio:m4a") in _buttons(bot.keyboards[-1])
    # Two per row, with the way back on its own: the tiers pair up naturally.
    assert [len(row) for row in bot.keyboards[-1].inline_keyboard] == [2, 1]


def test_a_photo_post_is_offered_nothing_else() -> None:
    """The image bug, pinned: a gallery link must not be offered audio or video
    tiers it cannot produce. Its one option is "send the media"."""
    for url in (
        "https://www.instagram.com/p/abc/",
        "https://www.tiktok.com/@user/photo/123",
        "https://x.com/user/status/12345/photo/1",
        "https://www.pinterest.com/pin/123/",
    ):
        routing = content_module.routing_for(url)

        assert [choice.label_key for choice in routing.choices] == ["fmt.media"], url
        assert routing.solo is not None, url


def test_a_video_or_music_link_is_never_answered_without_asking() -> None:
    for url in (
        "https://youtu.be/abc",
        "https://soundcloud.com/a/b",
        "https://x.com/user/status/12345",  # ambiguous: could be anything
    ):
        assert content_module.routing_for(url).solo is None, url


async def test_a_photo_post_is_downloaded_without_a_format_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A menu with one button that cannot be wrong is a menu that looks broken —
    and every tap costs a round trip. The link goes straight to the queue."""
    async def supported(url: str) -> bool:
        return True

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    bot = RecordingBot()
    message = _message("https://www.instagram.com/p/abc/", bot)
    queue = _fake_queue(depth=2)
    state = _fresh_state()

    await user_module._queue_url_flow(
        message,
        state,
        _user(),
        "https://www.instagram.com/p/abc/",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    assert bot.texts[0] == t("intake.analyse", FA)
    assert t("intake.photo_auto", FA) in bot.edits
    assert "موقعیت تقریبی: 2" in bot.edits[-1], "and it is already in the queue"
    assert t("intake.choose_media", FA) not in " ".join(bot.edits), "nothing was asked"
    assert bot.keyboards == [], "and there is nothing to tap"
    assert [task.media_format for task in queue.tasks] == ["video"]
    assert await state.get_state() is None, "a answered link leaves no pending step"


# ---------------------------------------------------------------------------
# Immediate feedback, and the queueing path
# ---------------------------------------------------------------------------


async def _state(url: str = "https://youtu.be/abc") -> FSMContext:
    context = _fresh_state()
    await context.set_state(DownloadStates.waiting_format)
    await context.update_data(url=url)
    return context


def _fresh_state() -> FSMContext:
    """A user with no step in progress (what a first link arrives with)."""
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


async def test_a_link_is_acknowledged_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot = RecordingBot()
    message = _message("https://youtu.be/abc", bot)

    await user_module._queue_url_flow(
        message,
        await _state(),
        _user(),
        "https://youtu.be/abc",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=_fake_queue(),
    )

    assert bot.texts[0] == t("intake.analyse", FA), "said first, before the probe"
    assert t("intake.choose_quality", FA) in bot.edits[-1], "and the same message becomes the question"
    assert ("🎬 بهترین کیفیت موجود", "fmt:video:best") in _buttons(bot.keyboards[-1])


async def test_a_file_link_is_never_handed_to_the_extractor_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CDN photo is not a site yt-dlp has a handler for, and it must not need one.

    The probe answers "does the extractor catalogue know this site?" — asking it
    about ``pbs.twimg.com/media/…?format=jpg`` is how a perfectly good photo link
    ends in "this site is not supported" before anything was attempted.
    """
    asked: list[str] = []

    async def probe(url: str) -> bool:
        asked.append(url)
        return False  # what the real probe says about a CDN file URL

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module, "_probe_supported", probe)
    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    bot = RecordingBot()
    url = "https://pbs.twimg.com/media/GAbc123?format=jpg&name=large"
    queue = _fake_queue(depth=1)

    await user_module._queue_url_flow(
        _message(url, bot),
        _fresh_state(),
        _user(),
        url,
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=queue,
    )

    assert asked == [], "the router already knew, so nothing was asked"
    assert t("intake.photo_auto", FA) in bot.edits
    assert [task.media_format for task in queue.tasks] == ["video"]
    assert bot.keyboards == [], "and no format menu was drawn for it"


async def test_a_page_link_is_still_put_to_the_extractor_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The skip is for links that are *files*; a page still gets the cheap check."""
    asked: list[str] = []

    async def probe(url: str) -> bool:
        asked.append(url)
        return False

    monkeypatch.setattr(user_module, "_probe_supported", probe)
    bot = RecordingBot()

    await user_module._queue_url_flow(
        _message("https://example.com/x", bot),
        _fresh_state(),
        _user(),
        "https://example.com/x",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=_fake_queue(),
    )

    assert asked == ["https://example.com/x"]
    assert "پشتیبانی نمی‌شود" in bot.edits[-1]


async def test_an_unsupported_link_answers_in_the_same_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unsupported(url: str) -> bool:
        return False

    monkeypatch.setattr(user_module, "_probe_supported", unsupported)
    bot = RecordingBot()
    message = _message("https://example.com/x", bot)

    await user_module._queue_url_flow(
        message,
        await _state(),
        _user(),
        "https://example.com/x",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=_fake_queue(),
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


def test_the_home_screen_is_navigation_only() -> None:
    """Home is a hub: account actions live under the screen that owns them, and
    the endpoints that were removed for good (`menu:status`, `menu:subscribe`)
    must not come back — a button is a promise, and every promise needs a
    handler that keeps it."""
    offered = {
        data for _, data in _buttons(user_module._main_menu(FA, admin=True, support=True))
    }

    assert offered == {
        "menu:download",
        "menu:profile",
        "menu:help",
        "menu:support",
        "menu:admin",
    }
    assert not offered & {"menu:status", "menu:subscribe"}
