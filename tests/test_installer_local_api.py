"""Zero-friction Local Telegram API setup: offered by the installer, documented in `.env.example`.

The cloud API refuses bot uploads over 50 MB; a local ``telegram-bot-api`` server
raises the ceiling to 2000 MB. The friction was the configuration — two keys from
https://my.telegram.org and a base URL nobody remembers. This pins the contract
that removes it (in the established style of ``tests/test_installer_gate.py`` and
``tests/test_cookie_mount.py``: deployment files have no shell harness here, so
what is tested is what the files promise). The prompt must stay *optional* —
the installer is run unattended as often as by hand — and a yes must write all
three settings, without ever leaving the duplicate-key mess a blind append would.
"""

from __future__ import annotations

from core.config import BASE_DIR

INSTALLER = BASE_DIR / "deploy" / "install.sh"
ENV_EXAMPLE = BASE_DIR / ".env.example"


def _script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _example() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


def test_the_example_names_the_three_keys_and_the_profile() -> None:
    example = _example()
    assert "TELEGRAM_API_ID=" in example
    assert "TELEGRAM_API_HASH=" in example
    assert "TELEGRAM_API_BASE_URL=http://telegram-api:8081" in example
    assert "local-api" in example, "the comment block names the compose profile that serves it"
    assert "deploy/install.sh" in example, "and the installer that can write the keys for you"


def test_the_installer_offers_the_setup_without_ever_forcing_it() -> None:
    script = _script()
    assert "[y/N]" in script, "optional — the default answer is no"
    assert "[ -t 0 ]" in script, "a piped or unattended run stays non-interactive"
    assert "read -r" in script
    assert "TELEGRAM_API_ID" in script and "TELEGRAM_API_HASH" in script


def test_a_yes_writes_all_three_settings_into_env() -> None:
    script = _script()
    assert 'set_env_key TELEGRAM_API_ID "$api_id"' in script
    assert 'set_env_key TELEGRAM_API_HASH "$api_hash"' in script
    assert 'set_env_key TELEGRAM_API_BASE_URL "http://telegram-api:8081"' in script
    assert ">> .env" in script, "a key the file does not carry is appended"
    assert "grep -q" in script and "sed " in script, (
        "a key .env already carries is filled in place — never duplicated, "
        "because a second line is dead weight to every reader of the file"
    )
    assert "local-api" in script, "the answer names how the server itself is started"
