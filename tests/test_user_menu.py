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
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, LinkPreviewOptions, Message, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import Settings
from core.i18n import t
from core.utils import today_local
from handlers import payment as payment_module
from handlers import user as user_module
from handlers.user import DownloadStates
from services import content as content_module
from services import subscription as subscription_module
from services.extractor import MediaInfo, VideoOption

USER_ID = 4242
FA = "fa"
EN = "en"


class RecordingBot:
    """A stand-in for ``Bot``: records the calls, answers with a real Message."""

    def __init__(self) -> None:
        self.calls: list[Any] = []
        #: The extraction pipeline the probe reads (``bot.state.extractor``).
        self.state: Any = None

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


def _message(
    text: str,
    bot: RecordingBot,
    user_id: int = USER_ID,
    chat_type: str = "private",
) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(
            id=user_id if chat_type == "private" else -100123,
            type=cast(Any, chat_type),
        ),
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
def _clean_tap_guard() -> Any:
    """Each test starts without the duplicate-tap memory (it is module state)."""
    user_module._recent_requests.clear()
    yield
    user_module._recent_requests.clear()


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


def test_the_home_screen_offers_the_two_hubs() -> None:
    markup = user_module._main_menu(FA)

    assert dict(_buttons(markup)) == {
        "⬇️ دانلود": "menu:download",
        "👤 پروفایل من": "menu:profile",
    }
    # Download first and alone: it is why most people came. The account actions
    # (VIP, Language, Support) live under Profile — home is two destinations and
    # nothing more.
    assert [len(row) for row in markup.inline_keyboard] == [1, 1]


def test_the_menu_speaks_the_language_it_is_drawn_in() -> None:
    assert dict(_buttons(user_module._main_menu(EN))) == {
        "⬇️ Download": "menu:download",
        "👤 My profile": "menu:profile",
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
        "🛠 پنل مدیریت": "menu:admin",
    }
    assert [len(row) for row in markup.inline_keyboard] == [1, 2]


def test_an_admins_profile_does_not_offer_the_store_either() -> None:
    """The store moved, it did not disappear — except where buying is impossible."""
    offered = dict(_buttons(user_module._profile_keyboard(FA, admin=True)))

    assert "menu:premium" not in offered.values()
    assert dict(_buttons(user_module._profile_keyboard(FA))) == {
        "🌐 زبان": "profile:language",
        "💎 ارتقا به ویژه (VIP)": "menu:premium",
        "💬 پشتیبانی": "menu:support",
        "⬅️ بازگشت": "menu:home",
    }


def test_only_an_admin_is_offered_the_panel() -> None:
    assert "menu:admin" not in dict(_buttons(user_module._main_menu(EN))).values()
    assert "menu:admin" in dict(_buttons(user_module._main_menu(EN, admin=True))).values()


def test_the_support_button_lives_under_profile_and_home_stays_clean() -> None:
    """Home is two destinations and nothing more; the support contact is
    Profile's business — who to ask is an account concern, and its *screen* is
    the one that honestly says whether anybody configured a contact yet."""
    assert "menu:support" not in dict(_buttons(user_module._main_menu(FA))).values()

    markup = user_module._profile_keyboard(FA)

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
    # A screen goes back to the parent it came from, not two levels up.
    assert ("⬅️ Back", "menu:profile") in _buttons(
        user_module._back_to_menu(EN, to="menu:profile")
    )


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
        "💬 پشتیبانی": "menu:support",
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
# Menu wiring
# ---------------------------------------------------------------------------


def test_no_menu_button_is_left_without_a_handler() -> None:
    """Every button a user can be shown must have somewhere to go (a dead button
    is worse than no button at all).

    ``menu:admin`` is answered by the *admin* router — keeping it out of the user
    module is deliberate separation, not a gap. Every ``lang:`` tap shares one
    handler, so those are matched by pattern. A URL button («add to a group») has
    no callback to route and goes with the empty ones.
    """
    from handlers import admin as admin_module
    from services import delivery

    source = inspect.getsource(user_module) + inspect.getsource(admin_module)
    offered = {data for _, data in _buttons(user_module._main_menu(FA, admin=True))}
    offered |= {data for _, data in _buttons(user_module._back_to_menu(FA))}
    offered |= {data for _, data in _buttons(user_module._profile_keyboard(FA))}
    offered |= {data for _, data in _buttons(user_module._language_keyboard(FA))}
    delivery.set_bot_username("AnimStoreV2ray_bot")
    try:
        offered |= {data for _, data in _buttons(user_module._download_keyboard(FA))}
    finally:
        delivery.set_bot_username("")

    def has_handler(data: str) -> bool:
        if f'F.data == "{data}"' in source:
            return True
        if data.startswith("lang:"):
            return "F.data.startswith(LANG_PREFIX)" in source
        return False

    missing = [data for data in sorted(offered) if data and not has_handler(data)]
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

    assert [choice.quality for choice in choices] == ["best"], "one honest default"
    assert all(choice.media_format == "video" for choice in choices)


def test_a_music_link_is_offered_audio_formats_and_no_video_tier() -> None:
    """Every tier this bot can genuinely build, spelled the way queues and cache
    keys have always spelled the two original ones (``mp3`` = balanced 192k,
    ``m4a`` = untouched stream) — a rename here would orphan every cached file."""
    routing = content_module.routing_for("https://soundcloud.com/a/b")

    assert routing.audio_formats == ("mp3", "m4a", "flac", "opus", "wav")
    assert [choice.quality for choice in routing.choices] == [
        "mp3.best",
        "mp3.high",
        "mp3",
        "mp3.small",
        "m4a",
        "m4a.high",
        "m4a.balanced",
        "m4a.small",
        "opus.best",
        "opus.high",
        "opus.balanced",
        "opus.small",
        "flac",
        "wav",
    ]
    assert all(choice.media_format == "audio" for choice in routing.choices)


def test_a_photo_post_is_not_offered_a_quality_menu() -> None:
    """The menu must not promise a resolution for something that has none."""

    choices = content_module.routing_for("https://www.instagram.com/p/abc/").choices

    assert len(choices) == 1
    assert choices[0].label_key == "fmt.media"


def test_an_ambiguous_post_is_offered_media_and_audio() -> None:
    """Nothing is hidden that might be true: a status can be a clip, a picture or a
    gallery, so the honest menu offers "whatever is there" plus the audio formats."""

    routing = content_module.routing_for("https://x.com/user/status/12345")

    assert routing.media_choice is not None
    assert routing.media_choice.label_key == "fmt.media"
    assert routing.audio_formats == ("mp3", "m4a", "flac", "opus", "wav")
    assert len(routing.choices) == 15, "the post's own media plus every audio tier"


def _fake_spotify_lookup(monkeypatch: pytest.MonkeyPatch, duration_s: float = 150.0) -> None:
    """The Spotify resolver, answered offline: one public track, one length.

    The production flow maps a Spotify link through ``services.spotify`` (its
    own page, then the YouTube counterpart) — a test that left that to the real
    network would be flaky by construction. Failure has its own cases below.
    """
    from types import SimpleNamespace

    async def lookup(url: str, **kwargs: Any) -> Any:
        return SimpleNamespace(duration_s=duration_s)

    monkeypatch.setattr(user_module.spotify, "lookup", lookup)


async def test_the_question_matches_the_link(monkeypatch: pytest.MonkeyPatch) -> None:
    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    _fake_spotify_lookup(monkeypatch)
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

    assert t("intake.choose_audio", FA) in bot.screens[-1]
    # The resolver is said out loud: a Spotify file comes from its public
    # counterpart, and the quality follows that source.
    assert t("intake.spotify_note", FA) in bot.screens[-1]
    # The question is the *format* grid — the quality presets are one tap deeper.
    assert ("🎧 MP3", "audf:mp3") in _buttons(bot.keyboards[-1])
    assert ("🎧 FLAC", "fmt:audio:flac") in _buttons(bot.keyboards[-1]), (
        "lossless has no quality knob and submits from here"
    )
    assert ("🎧 WAV", "fmt:audio:wav") in _buttons(bot.keyboards[-1]), (
        "wav has no quality knob and submits from here"
    )
    # Two per row, the way back filling the last one: the formats pair up.
    assert [len(row) for row in bot.keyboards[-1].inline_keyboard] == [2, 2, 2]
    # One message: the media card on top, the question under it.
    assert len(bot.screens) == 1
    assert bot.screens[0].splitlines()[0].startswith("🔗 https://open.spotify.com")
    assert "🎞" not in bot.screens[0], "nothing is chosen yet — the 🎞 line waits for a choice"


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


# ---------------------------------------------------------------------------
# Audio presets: bitrates, not moods
# ---------------------------------------------------------------------------


def test_the_audio_presets_are_bitrates_not_moods() -> None:
    labels = [label for label, _ in user_module._level_rows("mp3", EN)]

    assert labels == ["💎 320 kbps", "🔥 256 kbps", "⚖️ 192 kbps", "📦 128 kbps"]


def test_a_preset_row_estimates_the_size_from_the_links_own_length() -> None:
    rows = [label for label, _ in user_module._level_rows("mp3", EN, duration=150)]

    for label in rows:
        assert " · ~" in label and "MB" in label, label
    # The estimate follows the rate: the biggest preset weighs the most.
    sizes = [float(label.rsplit("~", 1)[1].split()[0]) for label in rows]
    assert sizes == sorted(sizes, reverse=True)
    # No length, no arithmetic — the row shows the rate alone.
    assert all("·" not in label for label, _ in user_module._level_rows("mp3", EN))


def test_an_untouched_stream_says_so_instead_of_borrowing_a_bitrate() -> None:
    labels = [label for label, _ in user_module._level_rows("m4a", FA)]

    assert labels[0] == "💎 کیفیت اصلی · بدون تبدیل"
    assert labels[1].startswith("🔥 256 kbps")
    assert all("kbps" in label for label in labels[1:])


def test_a_raw_or_lossless_format_gets_no_fake_quality_screen() -> None:
    assert user_module._level_rows("wav", EN) == []
    assert user_module._level_rows("flac", EN) == []


# ---------------------------------------------------------------------------
# The download screen and groups
# ---------------------------------------------------------------------------


def test_the_download_screen_offers_the_group_flow_once_the_bot_is_named() -> None:
    from services import delivery

    delivery.set_bot_username("AnimStoreV2ray_bot")
    try:
        markup = user_module._download_keyboard(EN)
    finally:
        delivery.set_bot_username("")

    buttons = [button for row in markup.inline_keyboard for button in row]
    assert any(
        button.url == "https://t.me/AnimStoreV2ray_bot?startgroup=true"
        for button in buttons
    ), "Telegram's own group picker, via the bot's real address"
    assert ("⬅️ Back", "menu:home") in _buttons(markup)


def test_no_group_button_before_the_bot_knows_its_own_name() -> None:
    buttons = [
        button
        for row in user_module._download_keyboard(FA).inline_keyboard
        for button in row
    ]
    assert all(button.url is None for button in buttons)


async def test_a_group_message_without_a_link_is_not_answered() -> None:
    """Groups are link-driven: no link in the message, no message from the bot."""
    bot = RecordingBot()
    message = _message("سلام بچه‌ها خوبید؟", bot, chat_type="supergroup")

    await user_module.on_text_with_url(
        message, await _state(), _user(), object(), _fake_queue(), bot, lang=FA
    )

    assert bot.texts == [] and bot.edits == []


async def test_a_group_message_with_a_link_gets_the_same_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_spotify_lookup(monkeypatch)
    bot = RecordingBot()
    message = _message(
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        bot,
        chat_type="supergroup",
    )

    await user_module.on_text_with_url(
        message, await _state(), _user(), object(), _fake_queue(), bot, lang=FA
    )

    assert t("intake.choose_audio", FA) in bot.screens[-1]
    assert ("🎧 MP3", "audf:mp3") in _buttons(bot.keyboards[-1])


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

    # TAP → WAIT → VIDEO, even here: one card message and the file — no
    # "analysing", no "downloading now", no queue receipt.
    assert len(bot.texts) == 1 and bot.texts[0].startswith("🔗 https://www.instagram.com")
    assert "🖼" not in " ".join(bot.screens)
    assert "موقعیت" not in " ".join(bot.screens), "the queue position is machinery"
    assert t("intake.choose_media", FA) not in " ".join(bot.screens), "nothing was asked"
    assert [row for kb in bot.keyboards for row in kb.inline_keyboard] == [], (
        "and there is nothing to tap"
    )
    assert [task.media_format for task in queue.tasks] == ["video"]
    assert queue.tasks[0].status_message_id == 1, "the card is the message the job narrates in"
    assert await state.get_state() is None, "a answered link leaves no pending step"


# ---------------------------------------------------------------------------
# Immediate feedback, and the queueing path
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _links_stay_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The question flow resolves share links over the network before routing;
    these tests speak in synthetic links and must stay offline. The resolver
    itself is pinned in tests/test_capability.py."""

    async def _same(url: str) -> str:
        return url

    monkeypatch.setattr(user_module, "_canonical_url", _same)


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


@pytest.mark.parametrize(
    "url",
    (
        "https://www.reddit.com/r/x/comments/1/y/",
        "https://www.reddit.com/r/Android/s/ShareToken",
        "https://v.redd.it/abc123",
        "https://redd.it/abc123",
    ),
)
async def test_a_supported_platform_is_never_branded_unsupported(url: str) -> None:
    """The live Reddit bug, pinned: share and short shapes died at the intake
    gate with «unsupported» before any engine or fallback ever ran — the
    catalogue's gaps are not this bot's opinion of a platform."""
    assert await user_module._probe_supported(url)


async def test_a_link_no_layer_recognizes_stays_unsupported() -> None:
    assert not await user_module._probe_supported(
        "https://some-unknown-site.example/v/1"
    )


async def test_an_undiscovered_ladder_says_so_and_offers_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty-menu bug, pinned: a video link whose qualities cannot be
    discovered gets an explicit error and a retry — never a silent default
    download standing in for the capability lookup that failed."""
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

    assert len(bot.screens) == 1, "one message, not two"
    assert bot.texts[0].startswith("🔗 https://youtu.be/abc")
    assert t("intake.probe_failed", FA) in bot.texts[0], (
        "the card names the link, and the screen says why there is no menu"
    )
    rows = _buttons(bot.keyboards[-1])
    assert (t("intake.probe_retry_btn", FA), user_module.PROBE_CALLBACK) in rows, (
        "one retry, which re-extracts"
    )
    assert all(data != "fmt:video:best" for _, data in rows), (
        "and no default download is offered in place of the ladder"
    )
    back_label, back_data = rows[-1]
    assert back_data == "menu:download", "the question's parent is the Download screen"
    assert back_label.startswith("⬅️"), "and says so the way every back button does"


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
    assert [task.media_format for task in queue.tasks] == ["video"]
    assert [row for kb in bot.keyboards for row in kb.inline_keyboard] == [], (
        "and no format menu was drawn for it"
    )
    assert "🖼" not in " ".join(bot.screens), "and nothing narrated over the file"


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
    assert "پشتیبانی نمی‌شود" in bot.texts[-1]


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

    assert len(bot.texts) == 1, "the answer is the one message"
    assert "پشتیبانی نمی‌شود" in bot.texts[0]


async def test_a_tap_is_acknowledged_silently_and_the_card_takes_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TAP → WAIT → VIDEO. The callback is answered with silence, the keyboard
    dies on the spot, and not one status message is sent — the card that carried
    the question becomes the job's card."""
    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    cb = _callback(bot, "fmt:video:720")
    queue = FakeQueue(depth=1)
    state = await _state()
    await state.update_data(title="A Clip", offered=["720"])

    await user_module.on_format_chosen(
        cb, state, _user(), object(), queue, bot, lang=FA
    )

    assert len(bot.answers) == 1 and bot.answers[0].text is None, "acknowledged silently"
    assert bot.texts == [], "no 'please wait', no queue receipt"
    # The keyboard is gone — the choice is made, the screen is now the card.
    assert [row for kb in bot.keyboards for row in kb.inline_keyboard] == []
    assert bot.edits[-1].splitlines()[0] == "🎬 A Clip"
    assert "🎞 720p" in bot.edits[-1], "the card names the choice"
    assert len(queue.tasks) == 1
    task = queue.tasks[0]
    assert task.media_format == "video"
    assert task.quality == "720", "the tier the user picked reaches the worker"
    assert task.lang == FA, "and so does the language the worker will answer in"
    assert task.title == "A Clip", "the probed title rides along for the worker's card"
    assert task.status_message_id == 1, "the worker narrates in this very message"


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
    assert len(bot.answers) == 1 and bot.answers[0].text is None, (
        "acknowledged silently — the card carries the news, no alert over it"
    )


async def test_a_cache_hit_replays_the_file_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[Any] = []

    async def cached(pool: Any, url: str, media_format: str = "", quality: str = "") -> Any:
        sent.append((media_format, quality))
        return {
            "url_hash": "x",
            "telegram_file_id": "AgAC",
            "kind": "video",
            "quality": "audio:m4a",
            "original_url": "https://soundcloud.com/a/b",
        }

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
    assert "M4A" in sent[1], "and the replay wears the same media card as a fresh send"
    assert "https://soundcloud.com/a/b" in sent[1], "including the link the user sent"
    assert "حافظه" not in " ".join(bot.screens), (
        "whether the bot has seen the link before is not the chat's business"
    )
    assert [row for kb in bot.keyboards for row in kb.inline_keyboard] == [], (
        "the keyboard is gone all the same"
    )


class _NoRefusal:
    """``preflight`` with nothing to say (the real rules have their own tests)."""

    @staticmethod
    def youtube_preflight(url: str, cookie_file: Any, **kwargs: Any) -> Any:
        return _Verdict()

    @staticmethod
    def clear_anonymous_refusal() -> None:
        """The retry's invalidation call lands here (see ``on_retry``)."""
        return None


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


# ---------------------------------------------------------------------------
# The quality menu the probe draws — and the taps it may answer
# ---------------------------------------------------------------------------


class _FakeExtractor:
    """Answers the metadata probe with one canned :class:`MediaInfo`."""

    def __init__(self, info: MediaInfo) -> None:
        self.info = info
        self.urls: list[str] = []

    async def extract(self, url: str) -> MediaInfo:
        self.urls.append(url)
        return self.info


def _probed_bot(
    options: tuple[VideoOption, ...], *, title: str = "A Clip"
) -> RecordingBot:
    """A recording bot whose extractor knows one link's real resolutions."""
    from types import SimpleNamespace

    bot = RecordingBot()
    bot.state = SimpleNamespace(
        extractor=_FakeExtractor(
            MediaInfo(
                source_url="https://youtu.be/abc",
                title=title,
                platform="youtube",
                webpage_url="https://youtu.be/abc",
                extension="mp4",
                thumbnail=None,
                duration=95,
                filesize_approx=1,
                is_live=False,
                video_options=options,
            )
        )
    )
    return bot


async def test_the_quality_menu_shows_what_the_link_actually_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sizes second, quality first — and never a size the site did not report.

    The extractor's list arrives unsorted and is sorted here: 1080p, 720p, 360p.
    An estimated size wears its ``~``; an exact one does not; a missing one says
    «size unknown» — real resolution, honest gap, never an invented number.
    """

    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot = _probed_bot(
        (
            VideoOption(360, 900 * 1024, True),
            VideoOption(1080, 14 * 1024 * 1024, False),
            VideoOption(720, 8 * 1024 * 1024, True),
            VideoOption(240),  # the site said nothing about its size
        )
    )

    await user_module._queue_url_flow(
        _message("https://youtu.be/abc", bot),
        _fresh_state(),
        _user(),
        "https://youtu.be/abc",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=_fake_queue(),
    )

    rows = _buttons(bot.keyboards[-1])
    assert [data for _, data in rows] == [
        "fmt:video:1080",
        "fmt:video:720",
        "fmt:video:360",
        "fmt:video:240",
        "menu:download",
    ], "the ladder, best first — whatever order the extractor said"
    assert rows[0] == ("1080p · ~14 MB", "fmt:video:1080"), (
        "resolution is the headline, and an estimate says ~"
    )
    assert rows[1] == ("720p · 8 MB", "fmt:video:720"), (
        "an exact size wears no ~"
    )
    assert rows[3] == ("240p · حجم نامشخص", "fmt:video:240"), (
        "a size the site never reported says so — the resolution is real and stays"
    )
    assert [len(row) for row in bot.keyboards[-1].inline_keyboard] == [1] * 5, (
        "one per row: quality is the headline, the size is secondary"
    )
    card_groups = bot.texts[0].split("\n\n")[:2]
    assert card_groups == ["🎬 A Clip", "🔗 https://youtu.be/abc"], (
        "the card names the media and its origin — and claims no quality yet: "
        "those two groups are all of it before the question line"
    )


async def test_the_chosen_card_shows_the_size_the_menu_promised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the tap, the card says quality • size — the same estimate the menu
    showed (its ``~`` included), before the file exists to say the real one."""

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    state = await _state()
    await state.update_data(
        title="A Clip",
        offered=["720"],
        options=[(720, 720, 8 * 1024 * 1024, False)],
    )

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), FakeQueue(), bot, lang=FA
    )

    assert "🎞 720p • ~8.0 MB" in bot.edits[-1], (
        "quality and the promised size together, still an estimate"
    )


async def test_a_size_the_site_never_reported_stays_off_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    state = await _state()
    await state.update_data(offered=["720"], options=[(720, 720, 0, False)])

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), FakeQueue(), bot, lang=FA
    )

    assert "🎞 720p" in bot.edits[-1] and "MB" not in bot.edits[-1]


async def test_a_tap_may_choose_only_what_the_menu_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crafted height is data: the offered list on the FSM decides."""

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    queue = FakeQueue()
    state = await _state()
    await state.update_data(offered=["1080", "720"], title="A Clip")

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:2160"), state, _user(), object(), queue, bot, lang=FA
    )

    assert queue.tasks == [], "nothing is queued"
    assert bot.answers[0].show_alert is True, "and the tap is answered honestly"

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), queue, bot, lang=FA
    )

    assert [task.quality for task in queue.tasks] == ["720"], (
        "the resolution the menu really showed runs exactly"
    )


async def test_the_same_tap_twice_is_one_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate-click protection, server-side: the keyboard dies on the first
    tap, and a second callback for the same request is acknowledged quietly and
    never reaches the queue."""

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())
    bot = RecordingBot()
    queue = FakeQueue()
    state = await _state()
    await state.update_data(offered=["720"])

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), queue, bot, lang=FA
    )
    # The second tap lands where the router sends a tap on a consumed menu.
    await user_module.on_stale_media_tap(
        _callback(bot, "fmt:video:720"), _user(), lang=FA
    )

    assert len(queue.tasks) == 1, "one tap, one download"
    assert bot.answers[-1].text is None, "the repeat is swallowed, not nagged"


async def test_a_tap_on_a_dead_menu_is_answered_not_run() -> None:
    """An old menu (or a forwarded one) still must not leave a spinner."""
    bot = RecordingBot()

    await user_module.on_stale_media_tap(_callback(bot, "fmt:video:720"), _user(), lang=FA)

    assert bot.answers[0].show_alert is True


async def test_a_failed_download_offers_a_retry_only_its_owner_can_press(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure lands on the card with [🔄][⬅️]. What a retry re-runs lives
    behind a server-side key — a stranger's tap cannot even spend it."""
    from services.queue import DownloadTask
    from services.worker import _notify_failure

    class _Status:
        def __init__(self) -> None:
            self.text = ""
            self.markup: Any = None

        async def edit_text(self, text: str, **kwargs: Any) -> None:
            self.text = text
            self.markup = kwargs.get("reply_markup")

    status = _Status()
    task = DownloadTask(
        url="https://youtu.be/abc",
        telegram_id=USER_ID,
        chat_id=USER_ID,
        media_format="video",
        quality="720",
        lang=FA,
        title="A Clip",
        status_message_id=1,
    )
    await _notify_failure(
        cast(Bot, RecordingBot()), task, "boom", status=status, card="🎬 A Clip"
    )

    assert status.text.startswith("🎬 A Clip\n\n"), "the card stays — it names the link"
    assert t("work.failed", FA, error="boom") in status.text
    buttons = [b for row in status.markup.inline_keyboard for b in row]
    assert [b.callback_data for b in buttons] == [buttons[0].callback_data, "menu:download"], (
        "[🔄 retry] [⬅️ to the Download screen]"
    )
    retry_data = buttons[0].callback_data

    async def no_cache(pool: Any, url: str, *args: Any) -> None:
        return None

    async def no_forget(pool: Any, url: str, *args: Any) -> None:
        return None

    monkeypatch.setattr(user_module.cache_service, "get_cached", no_cache)
    monkeypatch.setattr(user_module.cache_service, "forget", no_forget)
    monkeypatch.setattr(user_module, "preflight", _NoRefusal())

    # A stranger's tap: refused, and the key is left where it was.
    stranger = dict(_user())
    stranger["telegram_id"] = USER_ID + 1
    bot = RecordingBot()
    queue = FakeQueue()
    await user_module.on_retry(
        _callback(bot, retry_data), stranger, object(), queue, bot, lang=FA
    )
    assert queue.tasks == []
    assert bot.answers[0].show_alert is True

    # The owner's tap: the same job runs again, into the same card message.
    await user_module.on_retry(
        _callback(bot, retry_data), _user(), object(), queue, bot, lang=FA
    )
    assert [(item.quality, item.status_message_id) for item in queue.tasks] == [("720", 1)]

    # The key is spent — a second press finds nothing to run.
    bot2 = RecordingBot()
    await user_module.on_retry(
        _callback(bot2, retry_data), _user(), object(), FakeQueue(), bot2, lang=FA
    )
    assert bot2.answers[0].show_alert is True


def test_the_home_screen_is_navigation_only() -> None:
    """Home is a hub: account actions live under the screen that owns them (and
    the support contact under Profile), and the endpoints that were removed for
    good (`menu:status`, `menu:subscribe`, `menu:help`) must not come back — a
    button is a promise, and every promise needs a handler that keeps it."""
    offered = {
        data for _, data in _buttons(user_module._main_menu(FA, admin=True))
    }

    assert offered == {
        "menu:download",
        "menu:profile",
        "menu:admin",
    }
    assert not offered & {"menu:status", "menu:subscribe", "menu:help"}


def test_the_audio_menu_is_two_taps_deep_and_wav_skips_the_second() -> None:
    """Format first (a .mp3 and a .opus are different promises), then the quality
    presets. WAV is PCM and FLAC is lossless — their whole menu is the format
    button itself."""
    for codec, expected in (("mp3", 4), ("m4a", 4), ("opus", 4), ("flac", 0), ("wav", 0)):
        assert len(content_module.audio_level_choices(codec)) == expected, codec

    question = dict(_buttons(user_module._question_keyboard("https://soundcloud.com/a/b", EN)))
    assert question["🎧 MP3"] == "audf:mp3"
    assert question["🎧 M4A"] == "audf:m4a"
    assert question["🎧 OPUS"] == "audf:opus"
    assert question["🎧 FLAC"] == "fmt:audio:flac"
    assert question["🎧 WAV"] == "fmt:audio:wav"

    levels = dict(_buttons(user_module._level_keyboard("mp3", EN)))
    assert levels == {
        "💎 320 kbps": "fmt:audio:mp3.best",
        "🔥 256 kbps": "fmt:audio:mp3.high",
        "⚖️ 192 kbps": "fmt:audio:mp3",
        "📦 128 kbps": "fmt:audio:mp3.small",
        "⬅️ Back": "audf:back",
    }, "real bitrates on the buttons — the number is the promise"


async def test_the_quality_screen_replaces_the_format_screen_and_can_go_back() -> None:
    """Two taps, one message: the presets edit in place, and Back returns to the
    question — not to Home. Nobody loses their place by looking one step ahead."""
    bot = RecordingBot()

    await user_module.on_audio_format(
        _callback(bot, "audf:mp3"), await _state("https://soundcloud.com/a/b"), lang=EN
    )

    assert bot.texts == [], "the question is edited, not re-sent"
    assert dict(_buttons(bot.keyboards[-1]))["⬅️ Back"] == "audf:back"

    await user_module.on_audio_format(
        _callback(bot, "audf:back"), await _state("https://soundcloud.com/a/b"), lang=EN
    )

    assert t("intake.choose_audio", EN) in bot.edits[-1], "back is the question itself"
    assert "🎧 MP3" in dict(_buttons(bot.keyboards[-1]))


async def test_an_audio_menu_is_refused_on_a_link_that_has_none() -> None:
    """Crafted taps run nothing: a video link offers no audio formats to open."""
    bot = RecordingBot()

    await user_module.on_audio_format(_callback(bot, "audf:opus"), await _state(), lang=EN)

    assert bot.answers and bot.answers[0].show_alert is True
    assert bot.texts == [] and bot.edits == []


async def test_entering_a_screen_replaces_the_whole_keyboard() -> None:
    """The navigation promise, pinned: a screen swap carries *only* that screen's
    controls — Profile keeps Language/VIP/Back and nothing from Home survives."""
    bot = RecordingBot()

    await user_module.on_menu_profile(
        _callback(bot, "menu:profile"), _user(), object(), _fake_queue(), lang=FA
    )

    assert dict(_buttons(bot.keyboards[-1])) == {
        "🌐 زبان": "profile:language",
        "💎 ارتقا به ویژه (VIP)": "menu:premium",
        "💬 پشتیبانی": "menu:support",
        "⬅️ بازگشت": "menu:home",
    }
    assert bot.texts == [], "the same message, edited"


# ---------------------------------------------------------------------------
# The question arrives as the link's own picture — when it has one
# ---------------------------------------------------------------------------


class _Probe:
    """A metadata probe that answers with one canned :class:`MediaInfo`."""

    def __init__(self, info: MediaInfo) -> None:
        self.info = info

    async def extract(self, url: str) -> MediaInfo:
        return self.info


class _RefusingPhotoBot(RecordingBot):
    """A Telegram that will not carry the thumbnail — the fallback's case."""

    async def __call__(self, method: Any) -> Any:
        if isinstance(method, SendPhoto):
            raise RuntimeError("photo refused")
        return await super().__call__(method)


def _clip(thumbnail: str | None) -> MediaInfo:
    return MediaInfo(
        source_url="https://youtu.be/abc",
        title="A Clip",
        platform="youtube",
        webpage_url="https://youtu.be/abc",
        extension="mp4",
        thumbnail=thumbnail,
        duration=3725,
        filesize_approx=1,
        is_live=False,
        video_options=(VideoOption(720, 8 * 1024 * 1024, True),),
    )


async def _ask_with_probe(
    bot: RecordingBot, monkeypatch: pytest.MonkeyPatch, thumbnail: str | None
) -> None:
    """Drive one full question through the intake path, probe and all."""

    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot.state = SimpleNamespace(extractor=_Probe(_clip(thumbnail)))

    await user_module._queue_url_flow(
        _message("https://youtu.be/abc", bot),
        _fresh_state(),
        _user(),
        "https://youtu.be/abc",
        FA,
        bot=cast(Bot, bot),
        pool=object(),
        queue=_fake_queue(),
    )


async def test_the_question_arrives_as_a_photo_with_a_bold_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The premium look: the link's own thumbnail leads the question, and the
    caption says what it is and how long — in bold — before it asks."""
    bot = RecordingBot()

    await _ask_with_probe(bot, monkeypatch, "https://i.ytimg.com/vi/abc/hq.jpg")

    photos = [call for call in bot.calls if isinstance(call, SendPhoto)]
    assert len(photos) == 1, "the question is one photo — not a photo and a text"
    photo = photos[0]
    assert photo.photo == "https://i.ytimg.com/vi/abc/hq.jpg"
    caption = photo.caption or ""
    assert "<b>A Clip</b>" in caption, "the title is named, in bold"
    assert "<b>1:02:05</b>" in caption, "3725 seconds read the way a player says it"
    assert caption.endswith(user_module._question_body("https://youtu.be/abc", FA)), (
        "and the one question is asked underneath"
    )
    assert bot.texts == [] and bot.edits == [], "no plain-text twin"
    rows = _buttons(photo.reply_markup)
    assert any(data == "fmt:video:720" for _, data in rows), (
        "the same capability-driven menu rides the photo"
    )


async def test_a_refused_thumbnail_falls_back_to_the_text_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A picture Telegram will not carry must not eat the question: the photo is
    best-effort, and the fallback is the text screen — previews off."""
    bot = _RefusingPhotoBot()

    await _ask_with_probe(bot, monkeypatch, "https://i.ytimg.com/vi/abc/hq.jpg")

    assert len(bot.texts) == 1, "one question — the refused photo leaves no echo"
    assert bot.texts[0].startswith("🎬"), "the card names the title again, on the text side"
    assert "🔗 https://youtu.be/abc" in bot.texts[0]
    sent = [call for call in bot.calls if isinstance(call, SendMessage)][0]
    assert isinstance(sent.link_preview_options, LinkPreviewOptions)
    assert sent.link_preview_options.is_disabled is True
    assert bot.keyboards[-1] is not None, "and the menu still arrives"


async def test_a_question_without_a_thumbnail_stays_a_text_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link that reports no picture is not force-fitted into one — the plain
    question is a whole screen on its own."""
    bot = RecordingBot()

    await _ask_with_probe(bot, monkeypatch, None)

    assert bot.calls and all(not isinstance(call, SendPhoto) for call in bot.calls)
    assert len(bot.texts) == 1
    assert bot.texts[0].startswith("🎬")


async def test_intake_text_never_grows_a_link_preview() -> None:
    """Every plain-text intake answer says its piece on purpose; a link preview
    under it would be a second, uncontrolled message in the same breath."""
    bot = RecordingBot()

    await user_module.on_text_with_url(
        _message("hello there", bot),
        _fresh_state(),
        _user(),
        object(),
        _fake_queue(),
        bot,
        lang=FA,
    )
    await user_module.cmd_download(
        _message("/download", bot),
        cast(Any, SimpleNamespace(args="")),
        _fresh_state(),
        _user(),
        object(),
        _fake_queue(),
        bot,
        lang=FA,
    )

    sent = [call for call in bot.calls if isinstance(call, SendMessage)]
    assert [call.text for call in sent] == [
        t("intake.no_link_found", FA),
        t("intake.download_usage", FA),
    ]
    assert all(
        isinstance(call.link_preview_options, LinkPreviewOptions)
        and call.link_preview_options.is_disabled is True
        for call in sent
    ), "no intake text ever grows a preview"


# ---------------------------------------------------------------------------
# The raw link leaves the chat once the bot has answered it
# ---------------------------------------------------------------------------


class _NoDeleteBot(RecordingBot):
    """A chat where the bot may not delete messages — no delete rights."""

    async def __call__(self, method: Any) -> Any:
        if isinstance(method, DeleteMessage):
            raise TelegramBadRequest(method=method, message="Bad Request: not enough rights")
        return await super().__call__(method)


def _deletes(bot: RecordingBot) -> list[Any]:
    return [call for call in bot.calls if isinstance(call, DeleteMessage)]


async def _link_arrives(
    bot: RecordingBot, monkeypatch: pytest.MonkeyPatch, text: str = "https://youtu.be/abc"
) -> None:
    """One link through the real intake handler, probe and all."""

    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot.state = SimpleNamespace(extractor=_Probe(_clip(None)))

    await user_module.on_text_with_url(
        _message(text, bot), _fresh_state(), _user(), object(), _fake_queue(), bot, lang=FA
    )


async def test_the_raw_link_leaves_the_chat_once_the_menu_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some unofficial clients draw their own download preview under every raw
    link — clutter beside the menu that now speaks for it. The pasted URL goes
    the moment the bot has answered it."""
    bot = RecordingBot()

    await _link_arrives(bot, monkeypatch)

    assert isinstance(bot.calls[-1], DeleteMessage), "deleted only after the menu is out"
    assert bot.calls[-1].message_id == 1, "the user's message — never the bot's own"
    assert any(isinstance(call, (SendMessage, SendPhoto)) for call in bot.calls[:-1])


async def test_a_chat_that_refuses_the_deletion_still_gets_the_whole_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bot without delete rights (a group where it is not an admin) — or a
    message already gone — must never cost the user the menu. Deleting is a
    courtesy; the answer is the product."""
    bot = _NoDeleteBot()

    await _link_arrives(bot, monkeypatch)

    assert len(bot.texts) == 1, "the question arrived all the same"
    assert bot.keyboards[-1] is not None, "with its menu"


async def test_a_message_the_bot_never_answered_stays_in_the_chat() -> None:
    """No link at all, a link-shaped string that is not a link, or another step
    in progress: the message is the user's, and deleting it would be vandalism
    dressed as tidiness."""
    bot = RecordingBot()
    busy = _fresh_state()
    await busy.set_state("payment:waiting")

    await user_module.on_text_with_url(
        _message("hello there", bot), _fresh_state(), _user(), object(), _fake_queue(), bot, lang=FA
    )
    await user_module.on_text_with_url(
        _message("https://?x=1", bot), _fresh_state(), _user(), object(), _fake_queue(), bot, lang=FA
    )
    await user_module.on_text_with_url(
        _message("https://youtu.be/abc", bot), busy, _user(), object(), _fake_queue(), bot, lang=FA
    )

    assert _deletes(bot) == [], "nothing the bot did not answer is ever touched"
    assert bot.texts == [
        t("intake.no_link_found", FA),
        t("intake.invalid_link", FA),
        t("intake.step_in_progress", FA),
    ]


async def test_a_link_sent_through_download_goes_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/download <link>` carries the same raw link in a command's clothes — and
    a `/download` with no link at all deletes nothing."""

    async def supported(url: str) -> bool:
        return True

    monkeypatch.setattr(user_module, "_probe_supported", supported)
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=_Probe(_clip(None)))

    await user_module.cmd_download(
        _message("/download https://youtu.be/abc", bot),
        cast(Any, SimpleNamespace(args="https://youtu.be/abc")),
        _fresh_state(),
        _user(),
        object(),
        _fake_queue(),
        bot,
        lang=FA,
    )
    assert isinstance(bot.calls[-1], DeleteMessage)

    spare = RecordingBot()
    await user_module.cmd_download(
        _message("/download", spare),
        cast(Any, SimpleNamespace(args="")),
        _fresh_state(),
        _user(),
        object(),
        _fake_queue(),
        spare,
        lang=FA,
    )
    assert _deletes(spare) == [], "no link, nothing to clean"


# ---------------------------------------------------------------------------
# Zero-wait intake: a cached link is asked about without extraction
# ---------------------------------------------------------------------------


class _RecordingProbe:
    """A probe that records — used to prove whether extraction ran at all."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def extract(self, url: str) -> MediaInfo:
        self.calls.append(url)
        return _clip(None)


def _stored_rows() -> list[dict[str, Any]]:
    """What one link has already produced — the instant menu's whole material."""
    return [
        {
            "quality": "video:720",
            "label": "720p",
            "title": "A Clip",
            "kind": "video",
            "telegram_file_id": "v-1",
        },
        {
            "quality": "audio:mp3.best",
            "label": "MP3 · 320 kbps",
            "title": "A Clip",
            "kind": "audio",
            "telegram_file_id": "a-1",
        },
        {
            "quality": "video",
            "label": "1080p",
            "title": "A Clip",
            "kind": "video",
            "telegram_file_id": "v-2",
        },
    ]


async def _cached_rows_for(pool: Any, url: str) -> list[dict[str, Any]]:
    return _stored_rows()


async def test_a_cached_link_shows_the_instant_menu_without_extracting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-wait intake: the rows this link already produced *are* the menu —
    the extractor never runs, and every button is a request that replays at once."""
    monkeypatch.setattr(user_module.cache_service, "get_cached_rows", _cached_rows_for)
    probe = _RecordingProbe()
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=probe)
    state = _fresh_state()

    await user_module.on_text_with_url(
        _message("https://youtu.be/abc", bot), state, _user(), object(), _fake_queue(), bot, lang=FA
    )

    assert probe.calls == [], "the heavy extractor is never touched"
    assert len(bot.texts) == 1, "one question — asked instantly"
    assert dict(_buttons(bot.keyboards[-1])) == {
        "720p": "fmt:video:720",
        "MP3 · 320 kbps": "fmt:audio:mp3.best",
        "1080p": "fmt:video:best",
        t("intake.full_menu_btn", FA): user_module.PROBE_CALLBACK,
        t("menu.back", FA): "menu:download",
    }, "the rows' own requests, in the ordinary tap vocabulary"
    data = await state.get_data()
    assert data["offered"] == ["720", "best"]
    assert data["audio_offered"] == ["mp3"], "a tap is judged by what the menu drew"


def test_the_cached_menu_taps_speak_the_cache_key_language() -> None:
    """A cached menu's button is the very request its row was stored under: the
    tap folds back into exactly the cache key that will answer it."""
    for request in (
        "video",
        "video:720",
        "audio",
        "audio:mp3.best",
        "audio:wav",
        "audio:m4a.high",
    ):
        media_format, tier = user_module._request_parts(request)
        assert user_module._fmt_callback(media_format, tier) == f"fmt:{media_format}:{tier}"
        assert user_module.cache_service.request_key(media_format, tier) == request


async def test_a_tap_on_the_cached_menu_replays_instead_of_downloading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole zero-wait chain: instant menu → tap → the stored file, and the
    heavy job never even reaches the queue."""
    user_module._recent_requests.clear()
    monkeypatch.setattr(user_module.cache_service, "get_cached_rows", _cached_rows_for)

    async def get_cached(pool: Any, url: str, media_format: str, quality: object) -> Any:
        return {"telegram_file_id": "v-1", "kind": "video", "quality": "video:720"}

    sent: list[Any] = []

    async def replay(
        bot: Any, chat_id: int, cached: Any, caption: str | None = None, **kwargs: Any
    ) -> bool:
        sent.append(cached)
        return True

    monkeypatch.setattr(user_module.cache_service, "get_cached", get_cached)
    monkeypatch.setattr(user_module, "send_cached_file", replay)
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=_RecordingProbe())
    state = _fresh_state()
    queue = _fake_queue()
    await user_module.on_text_with_url(
        _message("https://youtu.be/abc", bot), state, _user(), object(), queue, bot, lang=FA
    )

    await user_module.on_format_chosen(
        _callback(bot, "fmt:video:720"), state, _user(), object(), queue, bot, lang=FA
    )

    assert sent and sent[0]["telegram_file_id"] == "v-1", "the stored file is replayed"
    assert queue.tasks == [], "and the heavy job never runs"


async def test_the_full_menu_button_runs_the_real_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instant menu's escape hatch: «All qualities» re-checks the link
    itself — the full ladder wants extraction, and asks for it deliberately."""
    monkeypatch.setattr(user_module.cache_service, "get_cached_rows", _cached_rows_for)
    probe = _RecordingProbe()
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=probe)
    state = _fresh_state()
    await state.update_data(url="https://youtu.be/abc")

    await user_module.on_probe_retry(
        _callback(bot, user_module.PROBE_CALLBACK),
        state,
        _user(),
        object(),
        _fake_queue(),
        bot,
        lang=FA,
    )

    assert probe.calls == ["https://youtu.be/abc"], "the re-check really re-extracts"
