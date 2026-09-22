"""Mounted cookie jar: read-only sources, live rotation, Docker placeholders.

The deployed shape mounts the project read-only at ``/cookies`` and reads the jar
from there, and yt-dlp *rewrites* its cookiefile when a download ends
(``YoutubeDL.close`` → ``save_cookies``) — so a read-only jar would fail every
download at the last step, after the bytes were already fetched. These tests pin
the writable copy that prevents it, the refresh that makes a fresh export live on
the next download (no restart, no rebuild), and the two ways a jar can be absent: a
missing file (fresh clone) and the empty directory Docker leaves behind for a
missing bind-mounted file.
"""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest
import yt_dlp

from core.config import BASE_DIR
from services import extractor as extractor_module
from services.extractor import ExtractorService, mount_info_for, parse_mountinfo, select_mount

HEADER = "# Netscape HTTP Cookie File\n"


def _row(name: str, value: str = "value") -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t2147483647\t{name}\t{value}\n"


def _jar(path: Path, *rows: str) -> Path:
    path.write_text(HEADER + "".join(rows or (_row("SAPISID"),)), encoding="utf-8")
    return path


def _service(download_dir: Path, cookie: Path) -> ExtractorService:
    return ExtractorService(download_dir, js_runtime="none", cookie_file=cookie)


def _make_read_only(path: Path) -> bool:
    """A jar with no write bit, as a read-only bind mount presents it.

    Returns ``False`` when the platform keeps the file writable anyway (running as
    root, or a filesystem that ignores the mode), because then the trap below
    cannot be reproduced and the test says so instead of failing.
    """
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return not os.access(path, os.W_OK)


def test_a_read_only_jar_is_handed_to_yt_dlp_as_a_writable_copy(tmp_path: Path) -> None:
    source = _jar(tmp_path / "cookies.txt")
    if not _make_read_only(source):
        pytest.skip("cannot make a file read-only in this environment")

    handed = Path(_service(tmp_path, source)._base_opts(extract_only=True)["cookiefile"])

    assert handed != source, "yt-dlp must never be pointed at the read-only mount"
    assert handed.parent == tmp_path / ".cookies"
    assert handed.is_file() and os.access(handed, os.W_OK)
    assert handed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_the_writable_copy_is_moved_into_place(tmp_path: Path) -> None:
    """yt-dlp runs in worker threads: nobody may read a half-written jar.

    A partial copy parses as "no cookies", which is indistinguishable from being
    logged out — and turns into exactly the block this project keeps chasing.
    """
    source = _jar(tmp_path / "cookies.txt")

    handed = Path(_service(tmp_path, source)._base_opts(extract_only=True)["cookiefile"])

    assert handed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert [path.name for path in (tmp_path / ".cookies").iterdir()] == ["cookies.txt"]


def test_a_failed_copy_everywhere_never_hands_over_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No space, no permission: yt-dlp gets *no* jar — never the read-only one.

    Handing the source over is exactly the reported production failure: yt-dlp
    rewrites its cookiefile at close, so a read-only mount ends every run in
    ``OSError: [Errno 30] Read-only file system: '/cookies/cookies.txt'``. The
    anonymous fallback is deliberate — and it says ``COOKIE_STORAGE`` once,
    loudly, with paths and errnos only.
    """
    source = _jar(tmp_path / "cookies.txt", _row("SAPISID", "old"))
    extractor = _service(tmp_path, source)
    copy_path = Path(extractor._base_opts(extract_only=True)["cookiefile"])

    _jar(source, _row("LOGIN_INFO"), _row("SAPISID", "new"))

    def failing_copy(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(extractor_module.shutil, "copyfile", failing_copy)

    with caplog.at_level("ERROR"):
        first = extractor._base_opts(extract_only=True)
        second = extractor._base_opts(extract_only=True)

    assert "cookiefile" not in first, "yt-dlp is never pointed at a jar it would rewrite"
    assert "cookiefile" not in second
    assert "old" in copy_path.read_text(encoding="utf-8"), "the previous copy is untouched"
    assert [path.name for path in copy_path.parent.iterdir()] == ["cookies.txt"]
    errors = [record.getMessage() for record in caplog.records if record.levelname == "ERROR"]
    storage_errors = [message for message in errors if "COOKIE_STORAGE" in message]
    assert len(storage_errors) == 1, "one loud error per jar, not one per call"
    assert "no space" in storage_errors[0] and str(source) in storage_errors[0]


def test_the_system_temp_dir_serves_when_the_download_area_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable downloads/ volume must not cost the login.

    The download area is the first copy location; a per-process directory under
    the system temp dir is the second. Only when *both* fail does a run go
    anonymous (the test above).
    """
    source = _jar(tmp_path / "cookies.txt")
    extractor = _service(tmp_path, source)
    real_copy = extractor_module.shutil.copyfile

    def download_area_is_read_only(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        if ".cookies" in Path(dst).parts:
            raise OSError(30, "Read-only file system")
        real_copy(src, dst)

    monkeypatch.setattr(extractor_module.shutil, "copyfile", download_area_is_read_only)

    handed = Path(extractor._base_opts(extract_only=True)["cookiefile"])

    assert handed != source and handed.parent != tmp_path / ".cookies"
    assert handed.parent.name.startswith("tdb-cookies-"), "the per-process runtime dir"
    assert handed.is_file() and os.access(handed, os.W_OK)
    assert handed.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_the_copy_is_what_yt_dlp_rewrites_when_a_download_ends(tmp_path: Path) -> None:
    """The regression this whole mechanism exists for."""
    source = _jar(tmp_path / "cookies.txt")
    if not _make_read_only(source):
        pytest.skip("cannot make a file read-only in this environment")

    opts = _service(tmp_path, source)._base_opts(extract_only=True)

    # Handed the mount, yt-dlp cannot save the jar back — and it does that on
    # every close, so this would fail every single download.
    with pytest.raises(OSError):
        yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "cookiefile": str(source)}).close()

    # Handed the bot's copy, the same close is uneventful.
    yt_dlp.YoutubeDL({**opts, "quiet": True, "no_warnings": True}).close()
    assert source.read_text(encoding="utf-8") == HEADER + _row("SAPISID")


def test_each_run_gets_a_private_copy_and_the_jar_never_moves(tmp_path: Path) -> None:
    """The rewrite-on-close lands on a private, disposable copy.

    The source and the shared snapshot survive every run byte-identical — that
    is what makes concurrent runs safe and the mount permanently read-only.
    """
    source = _jar(tmp_path / "cookies.txt", _row("LOGIN_INFO"), _row("SAPISID", "x"))
    extractor = _service(tmp_path, source)
    opts = extractor._base_opts(extract_only=True)
    snapshot = Path(opts["cookiefile"])
    before = source.read_text(encoding="utf-8")

    with extractor._ydl(opts) as ydl:
        run_copy = Path(ydl.params["cookiefile"])
        assert run_copy != snapshot and run_copy != source
        assert run_copy.is_file() and os.access(run_copy, os.W_OK)
        assert run_copy.read_text(encoding="utf-8") == before

    # The real close() above *saved* the jar back — proving the write lands on
    # the private copy: gone now, with both shared files untouched.
    assert not run_copy.exists(), "the private copy is removed with the run"
    assert source.read_text(encoding="utf-8") == before
    assert snapshot.read_text(encoding="utf-8") == before


def test_concurrent_runs_never_share_a_cookie_file(tmp_path: Path) -> None:
    """Worker A must never rewrite the jar worker C is reading.

    yt-dlp truncates-and-rewrites its cookiefile at close; runs sharing one file
    could catch it half-written — which parses as "no cookies" and turns into a
    bogus block. Every concurrent run therefore gets its own copy: proven here
    by four runs overlapping at a barrier, all seeing distinct writable files,
    all cleaned up afterwards, with the source and snapshot untouched.
    """
    source = _jar(tmp_path / "cookies.txt", _row("LOGIN_INFO"), _row("SAPISID", "x"))
    extractor = _service(tmp_path, source)
    opts = extractor._base_opts(extract_only=True)
    snapshot = Path(opts["cookiefile"])
    before = source.read_text(encoding="utf-8")

    runs = 4
    barrier = threading.Barrier(runs)
    seen: list[Path] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            with extractor._ydl(opts) as ydl:
                path = Path(ydl.params["cookiefile"])
                assert path.is_file() and os.access(path, os.W_OK)
                assert path.read_text(encoding="utf-8") == before
                seen.append(path)
                barrier.wait(timeout=30)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(runs)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, f"concurrent runs failed: {failures!r}"
    assert len(seen) == runs
    assert len(set(seen)) == runs, "no two runs share a cookie file"
    assert all(not path.exists() for path in seen), "every private copy is cleaned up"
    assert source.read_text(encoding="utf-8") == before
    assert snapshot.read_text(encoding="utf-8") == before
    assert [path.name for path in (tmp_path / ".cookies").iterdir()] == ["cookies.txt"]


def test_a_fresh_export_reaches_yt_dlp_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One exported jar replaced by another: the next download uses the new one."""
    source = _jar(tmp_path / "cookies.txt", _row("SAPISID", "old"))
    extractor = _service(tmp_path, source)
    first = Path(extractor._base_opts(extract_only=True)["cookiefile"])
    assert "old" in first.read_text(encoding="utf-8")

    copies: list[int] = []
    real_copy = extractor_module.shutil.copyfile

    def counting_copy(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        copies.append(len(copies) + 1)
        real_copy(src, dst)

    monkeypatch.setattr(extractor_module.shutil, "copyfile", counting_copy)

    # Unchanged source: reusing the copy keeps per-download overhead at one stat.
    assert Path(extractor._base_opts(extract_only=True)["cookiefile"]) == first
    assert copies == []

    # A re-export while the bot is running (a new login, a different size).
    _jar(source, _row("LOGIN_INFO", "fresh-login"), _row("SAPISID", "new"))
    again = Path(extractor._base_opts(extract_only=True)["cookiefile"])

    assert again == first
    assert "fresh-login" in again.read_text(encoding="utf-8")
    assert copies == [1]


def test_a_missing_jar_degrades_safely(tmp_path: Path) -> None:
    extractor = _service(tmp_path, tmp_path / "absent.txt")

    assert extractor.using_cookies is False
    assert "cookiefile" not in extractor._base_opts(extract_only=True)
    assert not (tmp_path / ".cookies").exists(), "nothing to copy, nothing created"

    extractor.warn_if_cookies_unusable()  # the startup path, must not raise


def test_dockers_placeholder_directory_is_named_as_such(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Docker creates an empty directory for a bind-mounted file that is missing.

    Still reachable: a host that ran the older file-level mount keeps that
    directory, and the message has to say what it is instead of "not a jar".
    """
    placeholder = tmp_path / "cookies.txt"
    placeholder.mkdir()
    extractor = _service(tmp_path, placeholder)

    assert extractor.using_cookies is False
    assert "cookiefile" not in extractor._base_opts(extract_only=True)

    with caplog.at_level("WARNING"):
        extractor.warn_if_cookies_unusable()
        extractor.warn_if_cookies_unusable()

    warnings = [r.getMessage() for r in caplog.records if "COOKIE_FILE" in r.getMessage()]
    assert len(warnings) == 1
    assert "directory" in warnings[0]
    assert "Docker" in warnings[0]


def test_a_missing_jar_is_a_note_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The fresh-clone case: no jar, no cookies, everything else still works."""
    extractor = _service(tmp_path, tmp_path / "cookies.txt")

    with caplog.at_level("WARNING"):
        extractor.warn_if_cookies_unusable()

    (message,) = [r.getMessage() for r in caplog.records if "COOKIE_FILE" in r.getMessage()]
    assert "does not exist" in message
    assert "export_cookies.py" in message


#: Real ``/proc/self/mountinfo`` text, trimmed: a Docker read-only bind of the
#: project (with an escaped space in the path) plus the root filesystem.
MOUNTINFO = (
    "25 30 0:23 / / rw,relatime shared:1 - overlay overlay rw,lowerdir=/a:/b\n"
    "36 25 0:31 /Users/mo/my\\040bot /cookies ro,relatime shared:9 - 9p C:\\134 rw\n"
)


def test_mountinfo_is_parsed_with_escapes_and_read_only_flags() -> None:
    root, cookies = parse_mountinfo(MOUNTINFO)

    assert cookies.point == "/cookies", "octal escapes in the mount point"
    assert cookies.root == "/Users/mo/my bot"
    assert cookies.source == "C:\\"
    assert cookies.filesystem == "9p"
    assert cookies.read_only is True
    assert root.read_only is False


def test_the_most_specific_mount_wins() -> None:
    entries = parse_mountinfo(MOUNTINFO)

    assert select_mount(entries, "/cookies/cookies.txt") is entries[1]
    assert select_mount(entries, "/app/main.py") is entries[0]
    assert select_mount(entries, "/cookies-ish") is entries[0]  # not a subtree


def test_mount_lookup_is_not_limited_to_posix_hosts(tmp_path: Path) -> None:
    """A Windows/macOS host has no mount table: describe, do not crash."""
    jar = _jar(tmp_path / "cookies.txt")
    info = mount_info_for(jar)

    assert info is None or info.point in str(jar)


def test_jar_state_reports_export_time_size_and_sync(tmp_path: Path) -> None:
    source = _jar(tmp_path / "cookies.txt", _row("SAPISID", "old"))
    extractor = _service(tmp_path, source)

    fresh = extractor.cookie_jar_state()
    assert fresh.kind == "ok"
    assert fresh.cookie_count == 1
    assert fresh.exported_at is not None
    assert fresh.in_sync is None, "nothing has been copied yet in a one-shot process"
    assert fresh.copy_path is None

    # A download hands yt-dlp a copy, which makes the answer exact from then on.
    extractor._base_opts(extract_only=True)
    synced = extractor.cookie_jar_state()
    assert synced.in_sync is True
    assert synced.copy_refreshed_at is not None

    # A newer export is not in use yet, and the report has to say so.
    _jar(source, _row("LOGIN_INFO", "new"), _row("SAPISID", "new"))
    stale = extractor.cookie_jar_state()
    assert stale.in_sync is False
    assert stale.cookie_count == 2


def test_jar_state_flags_a_read_only_jar(tmp_path: Path) -> None:
    source = _jar(tmp_path / "cookies.txt")
    if not _make_read_only(source):
        pytest.skip("cannot make a file read-only in this environment")

    state = _service(tmp_path, source).cookie_jar_state()

    assert state.kind == "ok"
    assert state.writable is False


def test_jar_state_names_a_missing_and_a_placeholder_path(tmp_path: Path) -> None:
    absent = _service(tmp_path, tmp_path / "cookies.txt").cookie_jar_state()
    assert absent.kind == "missing"
    assert absent.copy_path is None

    (tmp_path / "cookies.txt").mkdir()
    placeholder = _service(tmp_path, tmp_path / "cookies.txt").cookie_jar_state()
    assert placeholder.kind == "directory"


def test_the_cookie_jar_is_mounted_rather_than_baked_into_the_image() -> None:
    """The deployment contract behind "restart, don't rebuild".

    The mounted source is a directory on purpose: a *file* bind mount whose host
    path is missing turns into a directory and then fails container start with
    "not a directory", which is the state a fresh clone is in.
    """
    def directives(path: Path) -> list[str]:
        """Compose/ignore lines with comments dropped — the file explains itself."""
        return [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    compose = directives(BASE_DIR / "docker-compose.yml")

    assert ":/cookies:ro" in " ".join(compose)
    assert "COOKIE_FILE: /cookies/cookies.txt" in compose
    assert ":/app/cookies.txt" not in " ".join(compose), (
        "a file mount breaks container start when the jar is absent"
    )
    assert "cookies.txt" in directives(BASE_DIR / ".dockerignore"), (
        "a baked jar would go stale behind the mount"
    )
