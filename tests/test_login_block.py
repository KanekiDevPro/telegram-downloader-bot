"""A block caused by our own cookie jar, told to the user and to the admins.

"Sign in to confirm you're not a bot" reaches the user as a shrug and the admins
as nothing at all, although the cause is known and fixable. These tests pin the
rule that decides it is our jar (and not the site, and not the IP), the message
the user gets instead of a generic failure, and the once-per-window admin notice
that keeps a broken jar from paging the admins on every link.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from services import worker as worker_module
from services.extractor import ExtractionError, is_youtube_url, login_looking_block
from services.queue import DownloadTask

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str, domain: str = ".youtube.com") -> str:
    return f"{domain}\tTRUE\t/\tTRUE\t2147483647\t{name}\tvalue\n"


def _jar(path: Path, *rows: str) -> Path:
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def _logged_out_jar(path: Path) -> Path:
    """A jar with plausible cookies but no login: the recurring real failure."""
    return _jar(path, _row("PREF"), _row("CONSENT"), _row("__Secure-3PSID"))


def _logged_in_jar(path: Path) -> Path:
    return _jar(path, _row("LOGIN_INFO"), _row("SAPISID"), _row("PREF"))


def _block(code: str = "EXTRACTOR_BLOCKED") -> ExtractionError:
    return ExtractionError(code, "Sign in to confirm you're not a bot")


# ---------------------------------------------------------------------------
# Is this block ours to fix?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=abc", True),
        ("https://youtube.com/watch?v=abc", True),
        ("https://m.youtube.com/watch?v=abc", True),
        ("https://youtu.be/abc", True),
        ("https://www.youtube-nocookie.com/embed/abc", True),
        ("https://www.youtube.com.evil.example/watch", False),
        ("https://instagram.com/reel/abc", False),
        ("https://www.tiktok.com/@x/video/1", False),
        ("not a url", False),
    ],
)
def test_only_youtube_links_are_youtube(url: str, expected: bool) -> None:
    assert is_youtube_url(url) is expected


def test_no_usable_jar_explains_a_block_anywhere(tmp_path: Path) -> None:
    """Anonymous requests are the obvious cause, whatever the platform is."""
    missing = tmp_path / "cookies.txt"
    empty = _jar(tmp_path / "empty.txt")

    assert login_looking_block(_block(), "https://instagram.com/reel/1", missing)
    assert login_looking_block(_block(), "https://www.tiktok.com/@x/video/1", empty)
    assert login_looking_block(_block(), "https://www.youtube.com/watch?v=abc", None)


def test_a_logged_out_jar_explains_only_youtube(tmp_path: Path) -> None:
    """LOGIN_INFO is YouTube's rule; elsewhere a block is the site's own doing."""
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    assert login_looking_block(_block(), "https://youtu.be/abc", jar)
    assert not login_looking_block(_block(), "https://instagram.com/reel/1", jar)


def test_a_real_login_is_not_an_excuse(tmp_path: Path) -> None:
    """With a login in place a block is worth a proxy, not a re-export."""
    jar = _logged_in_jar(tmp_path / "cookies.txt")

    assert not login_looking_block(_block(), "https://youtu.be/abc", jar)


def test_a_stale_session_counts_and_other_failures_do_not(tmp_path: Path) -> None:
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    assert login_looking_block(_block("SESSION_STALE"), "https://youtu.be/abc", jar)
    for code in (
        "PRIVATE_VIDEO",
        "AGE_RESTRICTED",
        "UNSUPPORTED_URL",
        "GENERAL",
        "DRM_PROTECTED",  # a policy refusal is not an argument for re-exporting the jar
        "IMAGE_ONLY",  # neither is a post that has no video in it
    ):
        assert not login_looking_block(_block(code), "https://youtu.be/abc", jar), code


# ---------------------------------------------------------------------------
# What the user and the admins are told
# ---------------------------------------------------------------------------


class FakeBot:
    """Records sends, keyed by chat id."""

    def __init__(self, failing: tuple[int, ...] = ()) -> None:
        self.sent: list[tuple[int, str]] = []
        self.failing = set(failing)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if chat_id in self.failing:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))

    def texts_for(self, chat_id: int) -> list[str]:
        return [text for target, text in self.sent if target == chat_id]


def _task(chat_id: int = 42, url: str = "https://youtu.be/abc") -> DownloadTask:
    return DownloadTask(
        chat_id=chat_id,
        telegram_id=chat_id,
        url=url,
        media_format="video",
    )


@pytest.fixture(autouse=True)
def _reset_alert_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a clean rate-limit window."""
    monkeypatch.setattr(worker_module, "_last_login_block_alert_at", float("-inf"))
    monkeypatch.setenv("ADMIN_IDS", "1,2")


async def test_the_user_is_told_the_cause_not_a_shrug(tmp_path: Path) -> None:
    bot = FakeBot()
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    await worker_module._notify_login_block(bot, _task(chat_id=42), jar)  # type: ignore[arg-type]

    (message,) = bot.texts_for(42)
    assert "لاگین" in message and "ادمین" in message
    assert "دوباره بفرست" in message
    # Nothing the user cannot act on: no cookie names, no internal codes.
    assert "LOGIN_INFO" not in message and "EXTRACTOR_BLOCKED" not in message


async def test_the_admins_get_the_fix_and_the_failing_link(tmp_path: Path) -> None:
    bot = FakeBot()
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    await worker_module._notify_login_block(bot, _task(url="https://youtu.be/abc"), jar)  # type: ignore[arg-type]

    for admin_id in (1, 2):
        (hint,) = bot.texts_for(admin_id)
        assert "https://youtu.be/abc" in hint
        assert "LOGIN_INFO" in hint  # the actual missing cookie
        assert "/doctor" in hint
        assert "تازه" in hint  # the fix, in the operator's words


async def test_a_broken_jar_pages_the_admins_once_per_window(tmp_path: Path) -> None:
    """Every link fails while the jar is logged out; the admins hear it once."""
    bot = FakeBot()
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    for chat_id in (42, 43, 44):
        await worker_module._notify_login_block(bot, _task(chat_id=chat_id), jar)  # type: ignore[arg-type]

    assert len(bot.texts_for(1)) == 1, "one notice, not one per failed link"
    assert len(bot.texts_for(42)) == 1 and len(bot.texts_for(43)) == 1
    assert len(bot.texts_for(44)) == 1, "each user still gets their own answer"


async def test_the_admins_hear_again_after_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = FakeBot()
    jar = _logged_out_jar(tmp_path / "cookies.txt")
    await worker_module._notify_login_block(bot, _task(), jar)  # type: ignore[arg-type]

    monkeypatch.setattr(
        worker_module, "_last_login_block_alert_at", time.monotonic() - 3600.0
    )
    await worker_module._notify_login_block(bot, _task(chat_id=43), jar)  # type: ignore[arg-type]

    assert len(bot.texts_for(1)) == 2


async def test_a_missing_jar_is_still_reported_with_a_reason(tmp_path: Path) -> None:
    bot = FakeBot()

    await worker_module._notify_login_block(bot, _task(), tmp_path / "cookies.txt")  # type: ignore[arg-type]

    (hint,) = bot.texts_for(1)
    assert "ناشناس" in hint  # no jar at all → anonymous requests
    assert "/doctor" in hint


async def test_an_unreachable_admin_does_not_stop_the_user_message(tmp_path: Path) -> None:
    bot = FakeBot(failing=(1, 2))
    jar = _logged_out_jar(tmp_path / "cookies.txt")

    await worker_module._notify_login_block(bot, _task(chat_id=42), jar)  # type: ignore[arg-type]

    assert len(bot.texts_for(42)) == 1


def test_the_window_is_bounded_and_named() -> None:
    assert 60.0 <= worker_module.LOGIN_BLOCK_ALERT_INTERVAL_S <= 3600.0
