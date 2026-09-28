"""The compose stack's blast radius: published ports on the loopback only, and
the database password out of the file.

``docker-compose.yml`` once published Postgres (5432), Redis (6379) and the
local Bot API server (8081) on *every* interface of the host — a database with
a password baked into the same file (`downloader`), readable by anyone who
could reach the box. Two promises are pinned here, in the established style of
``tests/test_installer_local_api.py`` (deployment files have no shell harness,
so what is tested is what the files promise):

1. every published port binds ``127.0.0.1`` — the stack's own traffic is
   container-to-container and needs no host port at all; the published ones
   exist for a host-run bot and the operator's own tools;
2. the Postgres password is deployment material: compose *refuses to start*
   without ``POSTGRES_PASSWORD`` from ``.env``, the bot's ``DATABASE_URL``
   carries the same value, and the installers seed it (a generated secret, not
   a default every deployment would share).
"""

from __future__ import annotations

import re

from core.config import BASE_DIR

COMPOSE = BASE_DIR / "docker-compose.yml"
ENV_EXAMPLE = BASE_DIR / ".env.example"
INSTALLER = BASE_DIR / "install.sh"
DEPLOY_INSTALLER = BASE_DIR / "deploy" / "install.sh"


def _compose() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_no_published_port_reaches_past_the_loopback() -> None:
    compose = _compose()

    bare = re.findall(r'^\s*-\s*"(\d+:\d+)"\s*$', compose, re.M)
    assert bare == [], f"published on every interface of the host: {bare}"
    for binding in (
        '"127.0.0.1:5432:5432"',
        '"127.0.0.1:6379:6379"',
        '"127.0.0.1:8081:8081"',
    ):
        assert binding in compose, f"{binding} — the data stores stay reachable locally"


def test_the_database_password_is_an_environment_secret() -> None:
    compose = _compose()

    assert "downloader:downloader@" not in compose, "the hardcoded credential is gone"
    assert "POSTGRES_PASSWORD: downloader" not in compose
    assert "POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?" in compose, (
        "compose refuses to start until .env carries the secret — the error "
        "message names it"
    )
    assert "postgresql://downloader:${POSTGRES_PASSWORD:?" in compose, (
        "and the bot connects with the very same value"
    )


def test_the_redis_auth_option_reaches_the_client() -> None:
    """"loopback only" is the door; ``REDIS_PASSWORD`` is the lock — and a lock
    the client cannot open is an outage, so both halves move together."""
    compose = _compose()

    assert "--requirepass" in compose, "a set REDIS_PASSWORD turns AUTH on"
    assert "REDIS_URL: redis://:${REDIS_PASSWORD:-}@redis:6379/0" in compose, (
        "the bot authenticates with the same secret (an empty one authenticates "
        "nothing — the default deployment is unchanged)"
    )


def test_the_example_and_the_installers_seed_the_secret() -> None:
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=" in example
    assert "REDIS_PASSWORD=" in example

    installer = INSTALLER.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD=" in installer, (
        "the compose installer seeds it — a fresh .env without it cannot boot the stack"
    )
    assert re.search(r"openssl rand|/dev/urandom", installer), (
        "generated per deployment, never a default shared by everyone"
    )

    deploy = DEPLOY_INSTALLER.read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD" in deploy
    assert 'set_env_key POSTGRES_PASSWORD' in deploy
