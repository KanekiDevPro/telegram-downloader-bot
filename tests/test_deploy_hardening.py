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
import shutil
import subprocess
from pathlib import Path

import pytest

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


def test_the_operator_s_log_files_stay_out_of_git_status() -> None:
    """``install.sh`` writes a timestamped log beside the project on every
    install, update, start and restart (``install-*.log``, ``update-*.log``,
    ``start-*.log``, ``restart-*.log``) — operator evidence, never repository
    content. A stray shell redirect also keeps landing here as a file
    literally named ``user)``. The noise is *ignored*, never deleted: git
    status should show what the operator did to the code, not what the
    installer did to the directory."""
    ignore = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")

    assert "*.log" in ignore or all(
        f"{name}-*.log" in ignore for name in ("install", "update", "start", "restart")
    )
    assert "user)" in ignore


# ---------------------------------------------------------------------------
# E1: an older .env heals itself from .env.example (self-healing sync)
# ---------------------------------------------------------------------------
#
# The failure this removes: a release adds a mandatory variable (POSTGRES_PASSWORD
# once was one) and every existing deployment's `compose up` dies mid-build on an
# interpolation error. The installer heals the file *before* the build — missing
# keys appended with their example defaults, everything the operator wrote
# byte-identical.


def _sync_function() -> str:
    """The shipped helper, verbatim — what runs below is install.sh itself."""
    src = (BASE_DIR / "install.sh").read_text(encoding="utf-8")
    marker = "sync_missing_env_vars() {"
    assert marker in src, "install.sh defines sync_missing_env_vars"
    lines = src.splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if marker in line)
    depth = 0
    for index in range(start, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")
        if depth == 0:
            return "".join(lines[start : index + 1])
    raise AssertionError("sync_missing_env_vars is unterminated")


def _run_sync(tmp_path: Path, *, example: str, env: str | None) -> subprocess.CompletedProcess[str]:
    if shutil.which("bash") is None:
        pytest.skip("bash runs the shipped shell helper")
    (tmp_path / "sync.sh").write_text(_sync_function(), encoding="utf-8")
    (tmp_path / ".env.example").write_text(example, encoding="utf-8")
    existing = tmp_path / ".env"
    if env is None:
        if existing.exists():
            existing.unlink()
    else:
        existing.write_text(env, encoding="utf-8")
    return subprocess.run(
        ["bash", "-c", "source ./sync.sh && sync_missing_env_vars"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )


EXAMPLE = (
    "# example header\n"
    "BOT_TOKEN=\n"
    "POSTGRES_PASSWORD=example-secret\n"
    "REDIS_PASSWORD=\n"
    "DATABASE_URL=postgresql://downloader:CHANGE_ME@localhost:5432/downloader\n"
)
OLD_ENV = (
    "# the operator's own comment\n"
    "BOT_TOKEN=real-token\n"
    "DATABASE_URL=postgresql://downloader:mine@db:5432/downloader\n"
)


def test_an_older_env_gains_the_missing_secret_with_custom_values_untouched(
    tmp_path: Path,
) -> None:
    proc = _run_sync(tmp_path, example=EXAMPLE, env=OLD_ENV)

    assert proc.returncode == 0, proc.stderr
    assert "[INFO] Added missing configuration variable POSTGRES_PASSWORD to .env" in proc.stdout

    backups = list(tmp_path.glob(".env.bak-*"))
    assert len(backups) == 1, "exactly one atomic backup before any append"
    assert backups[0].read_text(encoding="utf-8") == OLD_ENV

    healed = (tmp_path / ".env").read_text(encoding="utf-8")
    assert healed.startswith(OLD_ENV), "comments, order and custom values are never rewritten"
    assert "POSTGRES_PASSWORD=example-secret" in healed, "the example default is the seed"
    assert "REDIS_PASSWORD=" in healed
    assert "# Auto-synced missing variables" in healed

    # Idempotent: a healed file syncs to silence, with no second backup.
    again = _run_sync(tmp_path, example=EXAMPLE, env=healed)
    assert again.returncode == 0
    assert again.stdout == ""
    assert list(tmp_path.glob(".env.bak-*")) == backups


def test_a_missing_env_is_seeded_from_the_example(tmp_path: Path) -> None:
    proc = _run_sync(tmp_path, example=EXAMPLE, env=None)

    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / ".env").read_text(encoding="utf-8") == EXAMPLE
    assert list(tmp_path.glob(".env.bak-*")) == [], "nothing to back up when nothing existed"


def test_the_installer_syncs_before_any_update_or_build() -> None:
    script = (BASE_DIR / "install.sh").read_text(encoding="utf-8")

    assert "sync_missing_env_vars() {" in script
    assert "cp .env .env.bak-$(date +%s)" in script, "the backup is atomic and timestamped"
    assert "# Auto-synced missing variables" in script
    assert "[INFO] Added missing configuration variable" in script

    update = script.split("do_update() {", 1)[1]
    assert "sync_missing_env_vars" in update.split("build_and_log", 1)[0], (
        "an update heals .env after the pull, before the rebuild"
    )
    build = script.split("build_and_log() {", 1)[1].split("\ndo_install() {", 1)[0]
    assert "sync_missing_env_vars" in build, "every build path heals first"


def test_the_host_installer_heals_its_env_too() -> None:
    deploy = (BASE_DIR / "deploy" / "install.sh").read_text(encoding="utf-8")

    assert "sync_missing_env_vars() {" in deploy
    assert deploy.index("sync_missing_env_vars", deploy.index("leaving it untouched")) < deploy.index(
        "set_env_key POSTGRES_PASSWORD"
    ), "an existing host .env heals before the password prompt"


# ---------------------------------------------------------------------------
# E2: the build is pre-flighted — compose config validates before up builds
# ---------------------------------------------------------------------------


def test_compose_config_validates_before_the_build() -> None:
    script = (BASE_DIR / "install.sh").read_text(encoding="utf-8")
    build = script.split("build_and_log() {", 1)[1].split("\ndo_install() {", 1)[0]

    assert "preflight_compose_config || return 1" in build
    assert build.index("preflight_compose_config") < build.index("compose up -d --build"), (
        "a broken interpolation fails here — not mid-build after images pulled"
    )

    preflight = script.split("preflight_compose_config() {", 1)[1].split("\n}\n", 1)[0]
    assert "compose config --quiet" in preflight, "validation, not output"
    assert ".env.example" in preflight, "the diagnostic names where the defaults live"
    assert "POSTGRES_PASSWORD" in preflight, "the diagnostic names the usual suspect"
