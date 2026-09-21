"""Regression tests for the settings layer.

These lock in the fixes for two startup-breaking bugs:

* ``ADMIN_IDS=1,2`` raised ``SettingsError`` (pydantic-settings demanded JSON),
  exactly as the shipped ``.env.example`` documented it.
* an empty ``COOKIE_FILE=`` became ``Path('.')``, which is truthy on disk and
  was handed to yt-dlp as a cookiejar.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from core import config as config_module
from core.config import BASE_DIR, CLOUD_API_UPLOAD_LIMIT_MB, Settings, sanitize_env_text


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Build settings from just these env vars (no .env file)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ADMIN_IDS
# ---------------------------------------------------------------------------

def test_admin_ids_accepts_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_IDS="123456789, 987654321")
    assert settings.admin_ids == [123456789, 987654321]


def test_admin_ids_accepts_whitespace_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, ADMIN_IDS="111 222").admin_ids == [111, 222]


def test_admin_ids_accepts_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, ADMIN_IDS="[333, 444]").admin_ids == [333, 444]


def test_admin_ids_blank_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, ADMIN_IDS="").admin_ids == []


def test_admin_ids_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, ADMIN_IDS="not-an-id")


def test_admin_ids_error_says_what_to_write_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare pydantic error is not something an operator can act on at 3am."""
    with pytest.raises(ValidationError) as caught:
        _settings(monkeypatch, ADMIN_IDS="123456789, oops")

    message = str(caught.value)
    assert "oops" in message and "123456789" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        # docker-compose's `environment:` block, `docker run -e` and a shell
        # `export` all pass the quotes through, and the dotenv parser does not.
        ('"8116519481"', [8116519481]),
        ("'8116519481'", [8116519481]),
        # One per line, semicolons, a trailing separator: the separators people type.
        ("1;2", [1, 2]),
        ("1\n2\n3", [1, 2, 3]),
        ("7,", [7]),
        ("7, ,8", [7, 8]),
        # A JSON list with ids as strings is a list of ids.
        ('[9, "10"]', [9, 10]),
    ),
)
def test_admin_ids_accepts_every_shape_an_operator_writes(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[int]
) -> None:
    assert _settings(monkeypatch, ADMIN_IDS=raw).admin_ids == expected


def test_is_admin_compares_ids_not_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug that makes an operator invisible to their own bot.

    Ids reach the code from three places — the environment (text), an asyncpg row
    (int) and JSON payloads (either) — and ``"123" in {123}`` is False, which does
    not look like a type error. It looks like a bot that ignores its admin.
    """
    settings = _settings(monkeypatch, ADMIN_IDS="8116519481")

    assert settings.is_admin(8116519481)
    assert settings.is_admin("8116519481")
    assert not settings.is_admin(8116519482)
    assert not settings.is_admin(None)
    assert not settings.is_admin("not-a-number")


# ---------------------------------------------------------------------------
# The fallback pool
# ---------------------------------------------------------------------------


def test_the_pool_is_the_embedded_instance_plus_the_public_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-config means a flagged VPS still has a second address to try."""
    settings = _settings(monkeypatch)

    assert settings.cobalt_endpoints == ("http://cobalt:9000", "https://api.cobalt.tools")
    assert settings.cobalt_enabled


def test_extra_instances_come_before_the_public_one(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, COBALT_FALLBACK_URLS="https://mirror.example, http://other:9000/")

    assert settings.cobalt_endpoints == (
        "http://cobalt:9000",
        "https://mirror.example",
        "http://other:9000",
        "https://api.cobalt.tools",
    )


def test_the_public_instance_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some operators would rather no link ever left their network."""
    settings = _settings(
        monkeypatch, COBALT_TRY_PUBLIC_INSTANCES="0", COBALT_FALLBACK_URLS="https://mirror.example"
    )

    assert settings.cobalt_endpoints == ("http://cobalt:9000", "https://mirror.example")


def test_blanking_every_address_switches_the_fallback_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public instance is a *backup*, never the whole fallback.

    Otherwise an operator who blanked ``COBALT_API_URL`` to switch the net off would
    silently get a stranger's instance serving their users' links instead.
    """
    settings = _settings(monkeypatch, COBALT_API_URL="")

    assert settings.cobalt_endpoints == ()
    assert not settings.cobalt_enabled


def test_a_pool_of_only_fallbacks_is_still_a_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, COBALT_API_URL="", COBALT_FALLBACK_URLS="https://mirror.example/")

    assert settings.cobalt_endpoints == ("https://mirror.example", "https://api.cobalt.tools")
    assert settings.cobalt_embedded is False


# ---------------------------------------------------------------------------
# Extractor retry knobs
# ---------------------------------------------------------------------------

def test_retry_defaults_match_the_shipped_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)

    assert settings.extractor_retry_attempts == 2
    assert settings.extractor_retry_backoff_s == 3.0


def test_blank_retry_values_fall_back_to_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """`KEY=` in .env means "default" — blank numbers must not crash startup."""
    settings = _settings(
        monkeypatch, EXTRACTOR_RETRY_ATTEMPTS="", EXTRACTOR_RETRY_BACKOFF_S="  "
    )

    assert settings.extractor_retry_attempts == 2
    assert settings.extractor_retry_backoff_s == 3.0


def test_retry_values_are_read_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, EXTRACTOR_RETRY_ATTEMPTS="0", EXTRACTOR_RETRY_BACKOFF_S="1.5")

    assert settings.extractor_retry_attempts == 0
    assert settings.extractor_retry_backoff_s == 1.5
    with pytest.raises(ValidationError):  # 6 retries would hold a worker slot for minutes
        _settings(monkeypatch, EXTRACTOR_RETRY_ATTEMPTS="6")


def test_is_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, ADMIN_IDS="42")
    assert settings.is_admin(42)
    assert not settings.is_admin(7)
    assert not settings.is_admin(None)


# ---------------------------------------------------------------------------
# COOKIE_FILE
# ---------------------------------------------------------------------------

def test_blank_cookie_file_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, COOKIE_FILE="").cookie_file is None


def test_cookie_file_placeholder_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, COOKIE_FILE="none").cookie_file is None


def test_relative_cookie_file_resolves_under_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    cookie_file = _settings(monkeypatch, COOKIE_FILE="cookies.txt").cookie_file
    assert cookie_file == BASE_DIR / "cookies.txt"
    assert cookie_file is not None and cookie_file.is_absolute()


def test_absolute_cookie_file_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    absolute = r"C:\data\cookies.txt" if os.name == "nt" else "/data/cookies.txt"
    assert _settings(monkeypatch, COOKIE_FILE=absolute).cookie_file == Path(absolute)


# ---------------------------------------------------------------------------
# DOWNLOAD_DIR
# ---------------------------------------------------------------------------

def test_download_dir_defaults_to_project_downloads() -> None:
    assert Settings(_env_file=None).download_dir == BASE_DIR / "downloads"  # type: ignore[call-arg]


def test_relative_download_dir_resolves_against_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped .env.example uses this relative value; it must not depend on CWD.
    assert _settings(monkeypatch, DOWNLOAD_DIR="downloads").download_dir == BASE_DIR / "downloads"


def test_blank_download_dir_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, DOWNLOAD_DIR="").download_dir == BASE_DIR / "downloads"


# ---------------------------------------------------------------------------
# WEBHOOK_PATH / sizes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("webhook", "/webhook"), ("/hook/", "/hook"), ("", "/webhook"), ("  /a/b ", "/a/b")],
)
def test_webhook_path_is_normalized(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    assert _settings(monkeypatch, WEBHOOK_PATH=raw).webhook_path == expected


def test_max_file_size_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, MAX_FILE_SIZE_MB="2000")
    assert settings.max_file_size_bytes == 2000 * 1024 * 1024


# ---------------------------------------------------------------------------
# Local Bot API server settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_telegram_api_id_is_zero(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    # The shipped .env.example leaves this empty; parsing must not explode.
    assert _settings(monkeypatch, TELEGRAM_API_ID=raw).telegram_api_id == 0


def test_telegram_api_id_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, TELEGRAM_API_ID="1234567").telegram_api_id == 1234567


def test_base_url_trailing_slash_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, TELEGRAM_API_BASE_URL="http://telegram-api:8081/")
    assert settings.telegram_api_base_url == "http://telegram-api:8081"
    assert settings.uses_local_api is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False), ("", False)],
)
def test_bool_flags_accept_friendly_values(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    # A blank value must mean "off" — not a ValidationError at startup.
    assert _settings(monkeypatch, TELEGRAM_API_LOCAL=raw).telegram_api_local is expected


def test_bool_flags_reject_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, TELEGRAM_API_LOCAL="maybe")


def test_blank_api_files_dir_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch, TELEGRAM_API_FILES_DIR="").telegram_api_files_dir is None


def test_relative_api_files_dir_resolves_under_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, TELEGRAM_API_FILES_DIR="telegram-files")
    assert settings.telegram_api_files_dir == BASE_DIR / "telegram-files"


# ---------------------------------------------------------------------------
# Inline comments in .env
# ---------------------------------------------------------------------------

def _settings_from_file(tmp_path: Path, text: str) -> Settings:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    return Settings(_env_file=env_file)  # type: ignore[call-arg]


def test_blank_value_with_inline_comment_stays_blank(tmp_path: Path) -> None:
    # python-dotenv keeps the comment as the value when there is no value before
    # it, which crashed int parsing on a .env that documents each key inline.
    settings = _settings_from_file(tmp_path, "TELEGRAM_API_ID=  # https://my.telegram.org\n")
    assert settings.telegram_api_id == 0


def test_blank_string_value_with_inline_comment_is_empty(tmp_path: Path) -> None:
    settings = _settings_from_file(tmp_path, "TELEGRAM_API_HASH=  # fill me in later\n")
    assert settings.telegram_api_hash == ""


def test_inline_comment_after_a_value_is_stripped(tmp_path: Path) -> None:
    settings = _settings_from_file(
        tmp_path,
        "BOT_MODE=polling  # polling or webhook\nTELEGRAM_API_BASE_URL=http://tg:8081  # local\n",
    )
    assert settings.bot_mode == "polling"
    assert settings.telegram_api_base_url == "http://tg:8081"


def test_hash_inside_quotes_is_kept(tmp_path: Path) -> None:
    settings = _settings_from_file(tmp_path, 'MANUAL_CARD_HOLDER="Ali #1"  # holder\n')
    assert settings.manual_card_holder == "Ali #1"


def test_whole_line_comments_are_left_alone(tmp_path: Path) -> None:
    settings = _settings_from_file(tmp_path, "# BOT_MODE=webhook\n\n   # indented\nBOT_MODE=polling\n")
    assert settings.bot_mode == "polling"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A=   # c", "A="),
        ("A=val # c", "A=val"),
        ('A="v # k" # c', 'A="v # k"'),
        ("A=#c", "A="),
        ("# A=b", "# A=b"),
        ("no equals sign", "no equals sign"),
    ],
)
def test_sanitize_env_text(text: str, expected: str) -> None:
    assert sanitize_env_text(text) == expected


def test_comment_absorbed_by_docker_compose_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # `docker compose`'s env_file parser keeps the comment for an empty value, so
    # it arrives as an environment variable rather than through a file.
    assert _settings(monkeypatch, TELEGRAM_API_ID="# from my.telegram.org").telegram_api_id == 0
    assert _settings(monkeypatch, TELEGRAM_API_HASH="  # fill me in").telegram_api_hash == ""
    assert _settings(monkeypatch, TELEGRAM_API_FILES_DIR="# optional").telegram_api_files_dir is None


def test_real_environment_values_are_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        BOT_TOKEN="123:abc",
        MANUAL_CARD_HOLDER="Ali",
        TELEGRAM_API_BASE_URL="http://tg:8081",
    )
    assert settings.bot_token == "123:abc"
    assert settings.manual_card_holder == "Ali"
    assert settings.telegram_api_base_url == "http://tg:8081"


def test_env_file_argument_is_honoured(tmp_path: Path) -> None:
    # Guards the source override in Settings.settings_customise_sources: the
    # rebuilt source must still obey _env_file (and _env_file=None).
    assert _settings_from_file(tmp_path, "QUEUE_NAME=from-temp-file\n").queue_name == "from-temp-file"
    assert Settings(_env_file=None).queue_name == "dl:tasks"  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Upload ceiling per transport
# ---------------------------------------------------------------------------

def test_upload_limit_follows_configured_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, MAX_FILE_SIZE_MB="2000")
    assert settings.upload_limit_mb == 2000
    assert settings.upload_limit_bytes == 2000 * 1024 * 1024


def test_cloud_fallback_caps_the_upload_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, MAX_FILE_SIZE_MB="2000")
    settings.use_cloud_api_fallback()
    assert settings.upload_limit_mb == CLOUD_API_UPLOAD_LIMIT_MB


def test_cloud_fallback_never_raises_a_lower_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch, MAX_FILE_SIZE_MB="20")
    settings.use_cloud_api_fallback()
    assert settings.upload_limit_mb == 20


# ---------------------------------------------------------------------------
# The fallback instance's address, per run mode
# ---------------------------------------------------------------------------

def test_the_compose_name_becomes_the_published_port_on_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One setting, two addresses — and both run modes are documented paths.

    The bot runs in the container and the scripts run on the host (``.dockerignore``
    keeps them out of the image), where ``cobalt`` does not resolve. The stack
    publishes the *same* instance on loopback, so that is where the probe goes —
    otherwise a healthy zero-config stack reports its own fallback as unreachable.
    """
    monkeypatch.setattr(config_module, "in_container", lambda: False)

    assert config_module.probe_url("http://cobalt:9000") == "http://127.0.0.1:9000"


def test_inside_the_network_the_service_name_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reverse translation would be wrong: in compose, loopback is the bot itself."""
    monkeypatch.setattr(config_module, "in_container", lambda: True)

    assert config_module.probe_url("http://cobalt:9000") == "http://cobalt:9000"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.cobalt.example",  # an instance somewhere else: not ours to move
        "http://127.0.0.1:9000",  # already the host-side address
        "",  # fallback off — nothing to translate
    ],
)
def test_anything_that_is_not_the_embedded_service_is_untouched(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setattr(config_module, "in_container", lambda: False)

    assert config_module.probe_url(url) == url


def test_an_explicit_port_and_credentials_survive_the_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the host changes: a URL that names its own port (or carries credentials)
    must not come back with either of them dropped."""
    monkeypatch.setattr(config_module, "in_container", lambda: False)

    assert config_module.probe_url("http://cobalt:9001") == "http://127.0.0.1:9001"
    assert (
        config_module.probe_url("http://user:pw@cobalt:9000")
        == "http://user:pw@127.0.0.1:9000"
    )


@pytest.mark.parametrize(
    ("service", "published"),
    [("cobalt", 9000), ("pot-provider", 4416), ("yt-session-generator", 8080)],
)
def test_every_helper_the_stack_ships_is_translated(
    monkeypatch: pytest.MonkeyPatch, service: str, published: int
) -> None:
    """Not just the fallback: the two YouTube helpers have a host-side address too."""
    monkeypatch.setattr(config_module, "in_container", lambda: False)
    url = f"http://{service}:{published}"

    assert config_module.probe_url(url) == f"http://127.0.0.1:{published}"
    assert config_module.COMPOSE_SERVICE_ENV[service] in {
        "COBALT_API_URL",
        "YTDLP_POT_PROVIDER_URL",
        "YOUTUBE_SESSION_SERVER",
    }


# ---------------------------------------------------------------------------
# The two helpers on YouTube's no-login route: shipped, and pointed at by default
# ---------------------------------------------------------------------------


def test_the_po_token_provider_defaults_to_the_shipped_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The default is only the default with nothing in the environment overriding it.
    monkeypatch.delenv("YTDLP_POT_PROVIDER_URL", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.ytdlp_pot_provider_url == "http://pot-provider:4416"
    assert settings.wants_pot_provider is True


def test_an_empty_provider_url_switches_the_route_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Off has to stay reachable: the extractor then runs exactly as it did before."""
    monkeypatch.setenv("YTDLP_POT_PROVIDER_URL", "")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.wants_pot_provider is False


def test_the_session_server_defaults_to_the_shipped_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("YOUTUBE_SESSION_SERVER", raising=False)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.youtube_session_server == "http://yt-session-generator:8080"
    assert settings.wants_session_server is True


def test_the_session_server_is_normalised_like_every_other_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing slash must not become part of a request path."""
    monkeypatch.setenv("YOUTUBE_SESSION_SERVER", " http://yt-session-generator:8080/ ")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.youtube_session_server == "http://yt-session-generator:8080"
