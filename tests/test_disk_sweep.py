"""The hourly disk sweep removes only what no live job can still need.

The defect class pinned here: the old ``_cleanup_stale_jobs`` deleted any
``job-*`` directory older than 24h by its *top-level* mtime, silently
(``except OSError: pass``), on the event loop — so a slow download whose
directory was created long ago but whose ``.part`` was written seconds ago
could lose its directory mid-flight, a ``job-*`` symlink could point the
remover outside ``DOWNLOAD_DIR``, and nobody ever heard about either.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from services import worker as worker_module


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Settings:
    """Settings pointed at a scratch download dir (no .env file)."""
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _use_settings(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)


def _job_dir(root: Path, *, name: str | None = None) -> Path:
    path = root / (name or f"job-{uuid.uuid4().hex[:10]}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _age(path: Path, seconds: float) -> Path:
    old = time.time() - seconds
    os.utime(path, (old, old))
    return path


def _sweep_now(root: Path, max_age_s: float) -> tuple[int, int, int]:
    return worker_module._sweep_download_dir(root, max_age_s, time.time())


# ---------------------------------------------------------------------------
# keep vs remove
# ---------------------------------------------------------------------------


def test_a_fresh_job_dir_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    fresh = _job_dir(tmp_path)
    (fresh / "video.mp4").write_bytes(b"x" * 16)

    worker_module._cleanup_stale_jobs()

    assert fresh.is_dir()


def test_a_stale_job_dir_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    stale = _job_dir(tmp_path)
    (stale / "video.mp4").write_bytes(b"x" * 16)
    _age(stale, 9000)
    _age(stale / "video.mp4", 9000)

    removed, errors, freed = _sweep_now(tmp_path, 7200)

    assert not stale.exists()
    assert (removed, errors) == (1, 0)
    assert freed == 16


def test_an_old_dir_with_one_fresh_file_inside_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Age is the *newest* mtime anywhere inside — a live ``.part`` keeps the
    whole directory young even when the directory itself is old."""
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    directory = _job_dir(tmp_path)
    live = directory / "video.mp4.part"
    live.write_bytes(b"partial")
    # Age the directory *after* creating the file: the creation itself bumps
    # the directory mtime, which would make this test pass for the wrong reason.
    _age(directory, 20000)

    removed, errors, _ = _sweep_now(tmp_path, 7200)

    assert directory.is_dir()
    assert (removed, errors) == (0, 0)


# ---------------------------------------------------------------------------
# the clamp
# ---------------------------------------------------------------------------


def test_a_small_configured_age_is_clamped_to_the_job_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``JOB_CLEANUP_MAX_AGE_S=60`` must not become a 60-second sweep: the
    floor is the longest a legitimate job may live."""
    settings = _settings(
        monkeypatch,
        tmp_path,
        JOB_LOCK_TTL_S="7200",
        DOWNLOAD_TIMEOUT_S="1800",
        JOB_CLEANUP_MAX_AGE_S="60",
    )
    _use_settings(monkeypatch, settings)
    # Older than the configured 60s, younger than the 7200s floor: a sweep
    # that honoured the raw value would delete this.
    directory = _job_dir(tmp_path)
    (directory / "video.mp4").write_bytes(b"x")
    _age(directory, 7000)
    _age(directory / "video.mp4", 7000)

    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        worker_module._cleanup_stale_jobs()

    assert directory.is_dir(), "the clamp keeps what the raw value would delete"
    clamped = [r for r in caplog.records if "JOB_CLEANUP_MAX_AGE_S" in r.message]
    assert len(clamped) == 1, "the clamp says so exactly once per sweep"


def test_the_floor_covers_both_the_claim_and_the_download_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        monkeypatch,
        tmp_path,
        JOB_LOCK_TTL_S="100",
        DOWNLOAD_TIMEOUT_S="1800",
        JOB_CLEANUP_MAX_AGE_S="1",
    )
    assert worker_module._job_cleanup_max_age_s(settings) == 2400


def test_the_new_setting_defaults_and_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _settings(monkeypatch, tmp_path).job_cleanup_max_age_s == 7200
    assert (
        _settings(monkeypatch, tmp_path, JOB_CLEANUP_MAX_AGE_S="36000").job_cleanup_max_age_s
        == 36000
    )


# ---------------------------------------------------------------------------
# names that are never ours
# ---------------------------------------------------------------------------


def test_names_outside_the_job_pattern_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    decoys = [
        _job_dir(tmp_path, name="job-backup"),
        _job_dir(tmp_path, name="job-XYZ"),
        _job_dir(tmp_path, name="notajob"),
    ]
    loose = tmp_path / "video.mp4"
    loose.write_bytes(b"x")
    cookies = tmp_path / ".cookies"
    cookies.mkdir()
    snapshot = cookies / "cookies.txt"
    snapshot.write_bytes(b"jar")
    for path in [*decoys, loose, snapshot]:
        _age(path, 30000)

    removed, errors, _ = _sweep_now(tmp_path, 60)

    assert (removed, errors) == (0, 0)
    assert all(path.exists() for path in [*decoys, loose, snapshot])


def test_orphaned_run_cookie_copies_are_swept_but_the_snapshot_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the per-run copy and ``_ydl``'s cleanup leaves
    ``.run-*.txt``/``.cookies-*.tmp`` behind; the ``cookies.txt`` snapshot is
    the live login and is never swept."""
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    cookies = tmp_path / ".cookies"
    cookies.mkdir()
    orphan = cookies / f".run-1234-{uuid.uuid4().hex}.txt"
    orphan.write_bytes(b"copy")
    partial = cookies / f".cookies-1234-{uuid.uuid4().hex}.tmp"
    partial.write_bytes(b"half")
    snapshot = cookies / "cookies.txt"
    snapshot.write_bytes(b"jar")
    for path in (orphan, partial, snapshot):
        _age(path, 30000)

    removed, errors, _ = _sweep_now(tmp_path, 60)

    assert (removed, errors) == (1 + 1, 0)
    assert not orphan.exists() and not partial.exists()
    assert snapshot.is_file()


# ---------------------------------------------------------------------------
# symlinks and the root
# ---------------------------------------------------------------------------


def test_a_symlink_to_an_outside_tree_is_neither_followed_nor_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "keep.me"
    victim.write_bytes(b"precious")
    link = tmp_path / f"job-{uuid.uuid4().hex[:10]}"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this platform cannot create symlinks here")
    _age(outside, 30000)

    removed, errors, _ = _sweep_now(tmp_path, 60)

    assert victim.is_file() and victim.read_bytes() == b"precious"
    assert link.is_symlink(), "the link itself is left alone"
    assert (removed, errors) == (0, 0)


def test_a_path_resolving_outside_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    assert worker_module._is_within(root / "job-abc", root)
    assert not worker_module._is_within(root / ".." / "escape", root)
    assert not worker_module._is_within(tmp_path / "elsewhere", root)


# ---------------------------------------------------------------------------
# errors are absorbed, counted, and said out loud
# ---------------------------------------------------------------------------


def test_a_removal_failure_is_absorbed_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    stale = _job_dir(tmp_path)
    _age(stale, 30000)

    def _boom(path: Path, **kwargs: Any) -> None:
        raise PermissionError(13, "denied", str(path))

    monkeypatch.setattr(worker_module.shutil, "rmtree", _boom)
    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        removed, errors, freed = _sweep_now(tmp_path, 60)

    assert (removed, errors, freed) == (0, 1, 0)
    assert stale.is_dir()
    assert any("disk sweep" in r.message for r in caplog.records) is False
    # ...the warning is logged by the wrapper, not the pure sweep:
    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        caplog.clear()
        worker_module._cleanup_stale_jobs()
    assert any("disk sweep" in r.message for r in caplog.records)


def test_an_unreadable_entry_keeps_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    directory = _job_dir(tmp_path)
    nested = directory / "nested"
    nested.mkdir()
    _age(directory, 30000)
    _age(nested, 30000)

    real_iterdir = Path.iterdir

    def _guarded(self: Path) -> Any:
        if self == nested:
            raise PermissionError(13, "denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _guarded)

    removed, errors, _ = _sweep_now(tmp_path, 60)

    assert directory.is_dir(), "what cannot be aged cannot be judged — keep it"
    assert (removed, errors) == (0, 1)


def test_a_real_permission_denied_entry_is_absorbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Linux gate runs this for real (uid 10001): a truly unreadable
    directory is kept, counted, and never raises."""
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs a non-root POSIX user")
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    directory = _job_dir(tmp_path)
    locked = directory / "locked"
    locked.mkdir()
    _age(directory, 30000)
    _age(locked, 30000)
    locked.chmod(0o000)
    if os.access(locked, os.R_OK | os.X_OK):
        pytest.skip("this platform does not enforce directory permissions")
    try:
        removed, errors, _ = _sweep_now(tmp_path, 60)
    finally:
        locked.chmod(0o700)

    assert directory.is_dir()
    assert (removed, errors) == (0, 1)


# ---------------------------------------------------------------------------
# logging discipline
# ---------------------------------------------------------------------------


def test_nothing_removed_means_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    _job_dir(tmp_path)

    with caplog.at_level(logging.INFO, logger=worker_module.__name__):
        worker_module._cleanup_stale_jobs()

    assert not [r for r in caplog.records if "disk sweep" in r.message]


def test_a_removal_logs_one_info_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    stale = _job_dir(tmp_path)
    (stale / "video.mp4").write_bytes(b"x" * 32)
    _age(stale, 30000)
    _age(stale / "video.mp4", 30000)

    with caplog.at_level(logging.INFO, logger=worker_module.__name__):
        worker_module._cleanup_stale_jobs()

    infos = [r for r in caplog.records if "disk sweep" in r.message and r.levelno == logging.INFO]
    assert len(infos) == 1


# ---------------------------------------------------------------------------
# the maintenance loop
# ---------------------------------------------------------------------------


def _stub_maintenance(monkeypatch: pytest.MonkeyPatch, stop: asyncio.Event) -> None:
    async def _zero(*args: Any, **kwargs: Any) -> int:
        stop.set()  # one cycle is enough: the wait below returns at once
        return 0

    async def _nothing(*args: Any, **kwargs: Any) -> None:
        return None

    async def _stop_after_first(*args: Any, **kwargs: Any) -> int:
        stop.set()
        return 0

    monkeypatch.setattr(worker_module.database, "expire_premiums", _zero)
    monkeypatch.setattr(worker_module.database, "prune_block_events", _nothing)
    monkeypatch.setattr(worker_module.database, "prune_helper_events", _nothing)
    monkeypatch.setattr(worker_module.database, "prune_stale_cache", _stop_after_first)


async def test_the_maintenance_loop_survives_a_sweep_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stop = asyncio.Event()
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))

    def _boom(*args: Any, **kwargs: Any) -> tuple[int, int, int]:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worker_module, "_sweep_download_dir", _boom)
    _stub_maintenance(monkeypatch, stop)

    with caplog.at_level(logging.ERROR, logger=worker_module.__name__):
        await asyncio.wait_for(worker_module.run_maintenance(stop, object()), timeout=10)

    assert stop.is_set()
    assert any("maintenance cycle failed" in r.message for r in caplog.records)


async def test_the_sweep_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = asyncio.Event()
    _use_settings(monkeypatch, _settings(monkeypatch, tmp_path))
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _record(*args: Any, **kwargs: Any) -> tuple[int, int, int]:
        seen.append(threading.get_ident())
        return (0, 0, 0)

    monkeypatch.setattr(worker_module, "_sweep_download_dir", _record)
    _stub_maintenance(monkeypatch, stop)

    await asyncio.wait_for(worker_module.run_maintenance(stop, object()), timeout=10)

    assert seen and seen[0] != loop_thread, "filesystem work must not block the loop"


def test_the_sweep_entry_point_is_synchronous() -> None:
    assert not asyncio.iscoroutinefunction(worker_module._sweep_download_dir)
    assert not asyncio.iscoroutinefunction(worker_module._cleanup_stale_jobs)
