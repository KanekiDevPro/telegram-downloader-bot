"""What the diagnostics command runs must be in the image it runs against.

The defect pinned here: ``install.sh diagnostics`` runs ``python
scripts/boot_check.py`` *inside the bot container*, but ``.dockerignore``
excluded the whole ``scripts/`` directory from the build context — so
``COPY . .`` built an image with no ``/app/scripts`` at all and the command
died with ``can't open file '/app/scripts/boot_check.py'``. The build context
now carries exactly that one script; the rest of ``scripts/`` stays host-side,
which is where those tools run (they read the checkout and the operator's
browser — see tests/test_config.py on scripts living on the host).
"""

from __future__ import annotations

from fnmatch import fnmatch

from core.config import BASE_DIR


def _dockerignore() -> list[str]:
    return (BASE_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()


def _installer() -> str:
    return (BASE_DIR / "install.sh").read_text(encoding="utf-8")


def _ignored(relpath: str, patterns: list[str]) -> bool:
    """Docker's .dockerignore verdict for one path: the last matching rule wins.

    Covers the shapes this file actually uses — comments, ``!`` exceptions,
    bare names, directory prefixes (``name/``) and ``*`` wildcards.
    """
    decision = False
    for raw in patterns:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:] if negated else line
        if pattern.endswith("/"):
            matched = relpath.startswith(pattern) or relpath == pattern[:-1]
        else:
            matched = fnmatch(relpath, pattern)
        if matched:
            decision = not negated
    return decision


def test_the_diagnostics_script_is_in_the_image_it_is_run_against() -> None:
    """The one script the installer runs in the container must survive the
    build context's slimming — every other script stays host-side."""
    patterns = _dockerignore()

    assert not _ignored("scripts/boot_check.py", patterns), (
        "install.sh diagnostics runs it inside the bot container"
    )
    assert _ignored("scripts/smoke.py", patterns), "the host-side tools stay host-side"


def test_the_installer_runs_the_diagnostics_script_it_ships() -> None:
    """Both halves of the mismatch, pinned together: what the installer invokes
    is what the build context carries."""
    installer = _installer()
    patterns = _dockerignore()

    assert "python scripts/boot_check.py" in installer
    assert not _ignored("scripts/boot_check.py", patterns)
