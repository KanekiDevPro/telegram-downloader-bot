"""The Appearance & Customization category: one home per aesthetic control.

Bot texts, button styles and platform toggles used to live in three different
corners of the panel (messages, system, downloads). They are one concern — how
the bot looks and speaks — so they share one category, and no other submenu
lists them anymore.
"""

from __future__ import annotations

from typing import Any

import pytest

from handlers import admin as admin_module

EN = "en"
FA = "fa"


def _buttons(markup: Any) -> list[tuple[str, str]]:
    return [
        (button.text or "", button.callback_data or "")
        for row in markup.inline_keyboard
        for button in row
    ]


def _destinations(markup: Any) -> list[str]:
    return [data for _label, data in _buttons(markup)]


def test_the_hub_has_an_appearance_category() -> None:
    offered = dict(_buttons(admin_module._panel_keyboard(FA)))

    assert offered["🎨 ظاهر، استایل و متون"] == "admin:cat_appearance"


def test_the_appearance_category_holds_texts_styles_and_toggles() -> None:
    labels = dict(_buttons(admin_module._category_keyboard(FA, "cat_appearance")))

    assert labels["🔤 ویرایش متون و پیام‌ها"] == "admin:texts"
    assert labels["🎨 رنگ و استایل دکمه‌ها"] == "admin:looks"
    assert labels["🎛 کلیدهای فعال پلتفرم‌ها"] == "admin:platforms"


def test_the_old_submenus_no_longer_list_them() -> None:
    messages = _destinations(admin_module._category_keyboard(EN, "cat_messages"))
    assert "admin:broadcast" in messages
    assert "admin:texts" not in messages
    downloads = _destinations(admin_module._category_keyboard(EN, "cat_downloads"))
    assert "admin:stats" in downloads
    assert "admin:platforms" not in downloads
    assert "admin:looks" not in _destinations(
        admin_module._category_keyboard(EN, "cat_system")
    )


def test_every_appearance_setting_has_exactly_one_home() -> None:
    seen: dict[str, str] = {}
    for category, items in admin_module._CATEGORY_ITEMS.items():
        for _label_key, destination in items:
            if destination in ("admin:texts", "admin:looks", "admin:platforms"):
                assert destination not in seen, (
                    f"{destination} lives in both {seen.get(destination)} and {category}"
                )
                seen[destination] = category

    assert set(seen) == {"admin:texts", "admin:looks", "admin:platforms"}
    assert set(seen.values()) == {"cat_appearance"}


async def test_the_moved_screens_walk_back_to_appearance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_looks(pool: Any) -> dict[str, tuple[str, str]]:
        return {}

    async def no_disabled(pool: Any) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(admin_module.database, "get_button_looks", no_looks)
    monkeypatch.setattr(admin_module.database, "get_disabled_platforms", no_disabled)

    _text, texts_keyboard = await admin_module.panel_screen(
        "texts", object(), None, None, lang="en"  # type: ignore[arg-type]
    )
    _look_text, looks_keyboard = await admin_module.panel_screen(
        "looks", object(), None, None, lang="en"  # type: ignore[arg-type]
    )
    _plat_text, platforms_keyboard = await admin_module.panel_screen(
        "platforms", object(), None, None, lang="en"  # type: ignore[arg-type]
    )

    for keyboard, screen in (
        (texts_keyboard, "texts"),
        (looks_keyboard, "looks"),
        (platforms_keyboard, "platforms"),
    ):
        assert "admin:cat_appearance" in _destinations(keyboard), screen
