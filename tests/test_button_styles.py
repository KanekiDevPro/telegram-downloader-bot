"""Telegram's own button appearance: ``style`` and ``icon_custom_emoji_id``.

Telegram lets a button wear one of its three colours and a custom emoji, and an
admin may set either per main button. Both are *decoration*, which is what fixes
the policy here: what is set reaches the keyboard (the button looks the way the
operator said), and what is nonsense never reaches Telegram at all — an unknown
colour must leave a plain button, never a send that fails and takes every user's
menu down with it.
"""

from __future__ import annotations

from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder

from core import ui


def test_a_colour_telegram_knows_reaches_the_button() -> None:
    assert ui.BUTTON_STYLES == ("primary", "success", "danger")
    assert ui.button_kwargs(style="primary") == {"style": "primary"}
    assert ui.button_kwargs(style="success") == {"style": "success"}
    assert ui.button_kwargs(style="danger") == {"style": "danger"}


def test_a_colour_telegram_does_not_know_is_dropped_not_sent() -> None:
    """A colour somebody typed wrong is not a broken menu."""
    assert ui.button_kwargs(style="rainbow") == {}
    assert ui.button_kwargs(style="PRIMARY") == {}
    assert ui.button_kwargs(style="") == {}
    assert ui.button_kwargs(style=None) == {}


def test_a_custom_emoji_id_must_look_like_one() -> None:
    assert ui.button_kwargs(icon_custom_emoji_id="5368324170671202286") == {
        "icon_custom_emoji_id": "5368324170671202286"
    }
    # Telegram's ids are digits; anything else would be refused by the API.
    assert ui.button_kwargs(icon_custom_emoji_id="not-an-id") == {}
    assert ui.button_kwargs(icon_custom_emoji_id=" ") == {}
    assert ui.button_kwargs(icon_custom_emoji_id=None) == {}


def test_a_dressed_button_carries_its_colour_and_its_emoji() -> None:
    builder = InlineKeyboardBuilder()
    ui.add_button(
        builder,
        "📥 Downloads",
        callback_data="menu:download",
        style="success",
        icon_custom_emoji_id="5368324170671202286",
    )

    button = builder.as_markup().inline_keyboard[0][0]

    assert (button.text, button.callback_data) == ("📥 Downloads", "menu:download")
    assert button.style == "success"
    assert button.icon_custom_emoji_id == "5368324170671202286"


def test_an_undressed_button_stays_plain() -> None:
    builder = InlineKeyboardBuilder()
    ui.add_button(builder, "📥 Downloads", callback_data="menu:download")

    button = builder.as_markup().inline_keyboard[0][0]

    assert button.style is None and button.icon_custom_emoji_id is None


def test_a_url_button_keeps_being_a_url_button() -> None:
    """The one URL button the bot draws is dressed by the same helper."""
    builder = InlineKeyboardBuilder()
    ui.add_button(
        builder, "👥 Add to a group", url="https://t.me/x?startgroup=1", style="primary"
    )

    button = builder.as_markup().inline_keyboard[0][0]

    assert button.url == "https://t.me/x?startgroup=1" and button.callback_data is None
    assert button.style == "primary"


def test_the_recolourable_set_is_the_main_buttons() -> None:
    """One list, pinned: every primary-menu button an admin may dress — the
    four on Home, the two account actions on Profile, the four Download
    sections, and the two Back buttons — each naming the text it is drawn
    from, so the panel and the menus cannot drift."""
    assert [callback for callback, _label_key in ui.MAIN_BUTTONS] == [
        "menu:download",
        "menu:profile",
        "menu:language",
        "menu:support",
        "menu:premium",
        "menu:topup",
        "menu:platform:youtube",
        "menu:platform:instagram",
        "menu:platform:spotify",
        "menu:platform:tiktok",
        "menu:home",
        "audf:back",
    ]
    assert [label_key for _callback, label_key in ui.MAIN_BUTTONS] == [
        "menu.download",
        "menu.profile",
        "menu.language",
        "menu.support",
        "menu.premium",
        "menu.topup",
        "download.youtube",
        "download.instagram",
        "download.spotify",
        "download.tiktok",
        "menu.back",
        "menu.back",
    ]


def test_a_stored_look_is_read_back_with_its_defaults() -> None:
    looks: ui.Looks = {"menu:download": ("success", "5368324170671202286")}

    assert ui.look_for(looks, "menu:download") == ("success", "5368324170671202286")
    assert ui.look_for(looks, "menu:profile") == ("", "")
    assert ui.look_for({}, "menu:download") == ("", "")
    # A row somebody edited by hand must not become a send Telegram refuses.
    assert ui.look_for({"menu:download": ("rainbow", "abc")}, "menu:download") == ("", "")
    hand_edited: Any = {"menu:download": "primary"}
    assert ui.look_for(hand_edited, "menu:download") == ("", "")


def test_the_download_sections_wear_the_colours_the_operator_chose() -> None:
    """A stored platform look reaches the Downloads picker — and only its own
    section button."""
    from handlers import user as user_module

    markup = user_module._download_keyboard(
        "en", looks={"menu:platform:spotify": ("success", "")}
    )
    buttons = [button for row in markup.inline_keyboard for button in row]
    spotify = next(
        item for item in buttons if item.callback_data == "menu:platform:spotify"
    )

    assert spotify.style == "success"
    assert all(
        item.style is None
        for item in buttons
        if item.callback_data != "menu:platform:spotify"
    )


def test_a_back_button_wears_its_destination_s_colour() -> None:
    """Back is a button too: the look stored for its destination dresses it,
    and no stored look leaves it plain."""
    from handlers import user as user_module

    def _style(markup: Any) -> Any:
        return markup.inline_keyboard[0][0].style

    assert (
        _style(user_module._back_to_menu("en", looks={"menu:home": ("danger", "")}))
        == "danger"
    )
    assert _style(user_module._back_to_menu("en")) is None
    assert (
        _style(
            user_module._back_to_menu(
                "en", to="menu:profile", looks={"menu:profile": ("primary", "")}
            )
        )
        == "primary"
    )


def test_a_garbage_look_on_a_new_button_stays_plain() -> None:
    """The fallback covers the whole set: nonsense for a platform or a Back
    button renders plain instead of breaking the keyboard."""
    from handlers import user as user_module

    assert ui.look_for({"menu:platform:tiktok": ("rainbow", "xx")}, "menu:platform:tiktok") == (
        "",
        "",
    )
    markup = user_module._download_keyboard(
        "en", looks={"menu:platform:tiktok": ("rainbow", "xx")}
    )
    tiktok = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data == "menu:platform:tiktok"
    )

    assert tiktok.style is None and tiktok.icon_custom_emoji_id is None
    back = user_module._back_to_menu("en", looks={"menu:home": ("rainbow", "xx")})
    assert back.inline_keyboard[0][0].style is None


def test_the_next_colour_in_the_cycle_is_telegram_s_own_order() -> None:
    assert [ui.next_style(style) for style in ("", "primary", "success", "danger")] == [
        "primary",
        "success",
        "danger",
        "",
    ]
    # An unknown stored colour restarts the cycle rather than propagating itself.
    assert ui.next_style("rainbow") == "primary"
