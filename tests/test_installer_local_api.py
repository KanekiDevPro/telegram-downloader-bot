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

import re

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


def test_the_server_reads_the_bot_s_downloads_off_the_shared_volume() -> None:
    """Zero-copy upload (see tests/test_zero_copy_upload.py) needs the
    telegram-api container to see the bot's job directory *at the same path*,
    read-only — and the bot to be told where that volume is. Without this
    wiring the file URI would name a file the server cannot open."""
    compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")

    assert "downloads:/app/downloads:ro" in compose
    assert "TELEGRAM_API_SHARED_DIR: /app/downloads" in compose


def test_lifecycle_commands_activate_the_local_api_profile() -> None:
    """The production failure behind "the local Bot API refused the file URI":
    ``telegram-api`` lives behind the ``local-api`` profile, and a profile-less
    ``docker compose up -d`` silently *skips* a running profile-gated container
    — so the bot got recreated with the new shared-volume env while the server
    kept its pre-volume mounts and could not open a single delivered file. Every
    lifecycle command in the root installer must therefore activate the profile
    whenever the local API is configured (and never before: an unconfigured
    telegram-api exits on start and crash-loops)."""
    script = (BASE_DIR / "install.sh").read_text(encoding="utf-8")

    assert '"--profile local-api"' in script
    assert "TELEGRAM_API_BASE_URL" in script, "the profile is gated on the local API being configured"


_SERVICE_KEY = re.compile(r"^  [A-Za-z0-9_-]+:\s*$|^[a-z][a-z0-9_-]*:\s*$", re.M)


def _service_block(compose: str, name: str) -> str:
    """One service's YAML block: from its key to the next key at the same or
    shallower indent (nested ``volumes:``/``environment:`` lists stay inside)."""
    header = re.search(rf"^  {re.escape(name)}:\s*$", compose, re.M)
    assert header is not None, f"{name} is missing from docker-compose.yml"
    rest = compose[header.end() :]
    end = _SERVICE_KEY.search(rest)
    return rest[: end.start()] if end else rest


def test_both_containers_mount_one_named_volume_at_one_path() -> None:
    """``file:///app/downloads/…`` names the same file in both containers only
    if both mount the *same named volume* at that exact path — the URI is built
    from the bot's view of the disk and resolved through the daemon's. "Bad
    Request: can't find real file path" is what it looks like when the two
    views diverge (a server kept on its pre-volume mounts while the bot was
    recreated around it). Pin the whole chain: one ``downloads`` volume,
    read-write on the bot, read-only on the server, and the shared-dir name
    equal to the mount target so the URI can never name a path the daemon
    lacks."""
    compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    server = _service_block(compose, "telegram-api")
    bot = _service_block(compose, "bot")
    volumes = compose.split("\nvolumes:", 1)[1]

    assert re.search(r"^  downloads:\s*$", volumes, re.M), "the named volume is declared"
    assert re.search(r"^ *- downloads:/app/downloads:ro\s*$", server, re.M), "read-only on the server"
    assert re.search(r"^ *- downloads:/app/downloads\s*$", bot, re.M), "read-write on the bot"
    assert "TELEGRAM_API_SHARED_DIR: /app/downloads" in bot
    assert "DOWNLOAD_DIR: /app/downloads" in bot
