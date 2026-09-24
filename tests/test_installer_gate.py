"""The installer's toolchain gate: both binaries, or no install.

``deploy/install.sh`` is the first thing a host install runs, and the pipeline
cannot work without both halves of the toolchain — ffmpeg converts (audio,
stream merges) and ffprobe verifies what was produced (``services/verify.py``).
The installer therefore hard-gates **both** before it touches the virtualenv,
and a missing one is an ``exit 1`` with the exact package to install — never a
warning that scrolls past.

Deployment scripts have no shell harness in this suite, so what is pinned here
is the script's contract in the project's established style (like
``tests/test_cookie_mount.py`` reading deployment files): both checks exist,
the failure is a hard exit with an actionable message, the old soft warning is
gone, and the gate runs before any install work.
"""

from __future__ import annotations

from core.config import BASE_DIR

INSTALLER = BASE_DIR / "deploy" / "install.sh"


def _script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_the_installer_checks_both_halves_of_the_toolchain() -> None:
    script = _script()
    assert "command -v ffmpeg" in script, "ffmpeg converts"
    assert "command -v ffprobe" in script, "ffprobe verifies the produced file"


def test_a_missing_binary_fails_early_with_an_actionable_message() -> None:
    script = _script()
    gate = script.split("if [ ! -d .venv ]")[0]  # everything before the install work
    assert "ERROR: required binaries missing:" in gate
    assert "exit 1" in gate, "a missing binary is a hard stop, not a warning"
    assert "apt-get install -y ffmpeg" in gate, "the message names the fix"
    assert "Then re-run: bash deploy/install.sh" in gate


def test_the_gate_runs_before_any_install_work() -> None:
    script = _script()
    assert script.index("command -v ffprobe") < script.index("pip install"), (
        "the toolchain is verified before the virtualenv is created or fed"
    )
    assert "WARNING: ffmpeg" not in script, "the old soft path is gone"
