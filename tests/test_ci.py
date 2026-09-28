"""CI installs what the repo resolved, not whatever the index serves today.

The workflow installed ``requirements.txt`` open-ended (``pydantic>=2.7`` and
friends), so every run resolved the newest releases: a red gate could mean
"upstream shipped something this morning", not "this commit broke something" —
and it only ever ran on ``push``, so a pull request's own commits were gated
at best retroactively. Three promises are pinned here (deployment files have
no shell harness; what is tested is what the files promise):

1. the run fires on pull requests as well as pushes;
2. CI installs from the committed lockfile, never from the open-ended
   requirement files;
3. the lock really covers every declared requirement — a lock missing a
   dependency would silently test a subset of the product.
"""

from __future__ import annotations

import re

from core.config import BASE_DIR

CI = BASE_DIR / ".github" / "workflows" / "ci.yml"
LOCK = BASE_DIR / "requirements.lock"
DECLARED = (BASE_DIR / "requirements.txt", BASE_DIR / "requirements-dev.txt")


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-")


def test_the_workflow_gates_pull_requests_and_installs_the_lock() -> None:
    ci = CI.read_text(encoding="utf-8")

    assert re.search(r"^\s*pull_request\s*:", ci, re.M), (
        "every PR is gated on its own commits, not only after they land"
    )
    assert "requirements.lock" in ci
    assert "-r requirements.txt" not in ci, (
        "the open-ended install is gone — the lock is what the index may serve"
    )


def test_the_lock_pins_every_declared_requirement() -> None:
    assert LOCK.is_file(), "the lock is committed, not generated per run"
    lock = LOCK.read_text(encoding="utf-8")
    pinned = {_normalize(name) for name in re.findall(r"^([A-Za-z0-9_.-]+)==", lock, re.M)}
    assert pinned, "the lock holds name==version lines"

    for path in DECLARED:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "-r")):
                continue
            name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0]
            assert _normalize(name) in pinned, (
                f"{name} is declared in {path.name} but not pinned in requirements.lock"
            )
