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


def test_the_example_names_the_three_keys_and_the_installer() -> None:
    example = _example()
    assert "TELEGRAM_API_ID=" in example
    assert "TELEGRAM_API_HASH=" in example
    assert "TELEGRAM_API_BASE_URL=http://telegram-api:8081" in example
    assert "--profile" not in example, "the server starts with the stack — no profile to remember"
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
    assert "docker compose up -d" in script, (
        "the answer names the plain start — the server comes up with the stack"
    )


def test_the_server_reads_the_bot_s_downloads_off_the_shared_volume() -> None:
    """Zero-copy upload (see tests/test_zero_copy_upload.py) needs the
    telegram-api container to see the bot's job directory *at the same path*,
    read-only — and the bot to be told where that volume is. Without this
    wiring the file URI would name a file the server cannot open."""
    compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")

    assert "downloads:/app/downloads:ro" in compose
    assert "TELEGRAM_API_SHARED_DIR: /app/downloads" in compose


def test_the_lifecycle_recreates_the_stack_without_profile_flags() -> None:
    """The production failure behind "the local Bot API refused the file URI",
    settled for good: a profile-gated ``telegram-api`` was silently *skipped*
    by a profile-less ``docker compose up -d`` while the bot was recreated
    around the new shared-volume mount — the two views of the disk drifted and
    every file URI was refused. The profile trap is gone (see
    ``test_the_server_is_a_first_class_compose_citizen``); what the lifecycle
    owes is that a volume, env or config change recreates the containers on the
    next ``up`` — ``--remove-orphans``, never a manual ``--force-recreate``."""
    script = (BASE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "--profile local-api" not in script, "the server is a first-class service — nothing to activate"
    assert "local_api_configured" not in script
    assert "up -d --build --remove-orphans" in script, (
        "a volume or config change must recreate containers on the next up — "
        "no --force-recreate, no manual CLI"
    )


def test_the_server_is_a_first_class_compose_citizen() -> None:
    """``telegram-api`` serves the zero-copy path and the 2000 MB ceiling, so it
    starts with the stack like bot, postgres and redis: every ``up`` keeps it —
    and its volume mounts — in step with the bot, which is what makes profile
    drift impossible rather than merely unlikely. Its healthcheck stays — and
    it is what the bot now *waits for*: booting against a still-starting server
    read as "unreachable", latched the cloud fallback and killed zero-copy for
    the whole run. ``service_healthy`` closes that race for good."""
    compose = (BASE_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    server = _service_block(compose, "telegram-api")
    bot = _service_block(compose, "bot")

    assert "profiles:" not in server, "nothing gates the server out of the default lifecycle"
    assert "healthcheck:" in server, "and it stays health-checked like the rest"
    assert re.search(r"telegram-api:\s*\n\s*condition: service_healthy", bot), (
        "the bot boots only after the server is healthy — a starting server must "
        "never look unreachable and latch the cloud fallback"
    )


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
