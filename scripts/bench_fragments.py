"""Fragment-concurrency benchmark — yt-dlp native downloader, ``N=1/2/4/8``.

Usage:  python scripts/bench_fragments.py [--url URL] [--quality 720]
                                           [--ns 1 2 4 8]

Downloads the *same* YouTube video at the *same* quality once per ``N`` using
the production yt-dlp options (``ExtractorService._base_opts``) with only
``concurrent_fragment_downloads`` varied, and reports wall time, average and
peak speed, plus failures — so a concurrency change is a measurement, never a
guess. aria2c is deliberately *not* in the matrix: yt-dlp only consults an
external downloader for plain http/ftp, while YouTube serves DASH/HLS
fragments to the native downloader (see ``_base_opts``); the Dockerfile still
ships aria2 for the plain-file case.

Stages follow the report vocabulary: T1 (probe) is measured once up front;
per run T3 (download, first byte → last ``finished`` hook) and T4 (merge /
post-processing, last ``finished`` → return) are split via progress hooks.
T5 (Telegram upload) needs the Bot API and is out of scope here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yt_dlp  # noqa: E402

from core.config import get_settings  # noqa: E402
from core.logging import force_utf8_console  # noqa: E402
from services.extractor import ExtractorService  # noqa: E402
from services.telemetry import safe_mbps  # noqa: E402

DEFAULT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _service(workdir: Path) -> ExtractorService:
    settings = get_settings()
    return ExtractorService(
        workdir,
        cookie_file=settings.cookie_file,
        proxy=settings.ytdlp_proxy,
        youtube_clients=settings.ytdlp_youtube_clients,
        remote_components=settings.ytdlp_remote_components,
        force_ipv4=settings.ytdlp_force_ipv4,
        cache_dir=settings.ytdlp_cache_dir,
    )


async def _probe(url: str, workdir: Path) -> tuple[float, int]:
    service = _service(workdir)
    started = time.monotonic()
    info = await service.extract(url)
    return time.monotonic() - started, len(info.video_options)


def _run_once(url: str, quality: str, fragments: int, workdir: Path) -> dict[str, Any]:
    service = _service(workdir)
    opts = service._base_opts(extract_only=False, media_format="video", quality=quality)
    opts["concurrent_fragment_downloads"] = fragments
    opts["outtmpl"] = str(workdir / f"n{fragments}" / "%(title).50s [%(id)s].%(ext)s")

    samples: list[tuple[float, int]] = []
    finished_at = 0.0
    errors = 0

    def hook(event: dict[str, Any]) -> None:
        nonlocal finished_at, errors
        status = event.get("status")
        if status == "downloading":
            samples.append((time.monotonic(), int(event.get("downloaded_bytes") or 0)))
        elif status == "finished":
            finished_at = time.monotonic()
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            samples.append((finished_at, int(total or 0)))
        elif status == "error":
            errors += 1

    opts["progress_hooks"] = [hook]
    started = time.monotonic()
    failure = ""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001 — the failure *is* the datum
        failure = f"{type(exc).__name__}: {exc}"
    total_s = time.monotonic() - started

    files = [path for path in workdir.rglob("*") if path.is_file()]
    size = max((path.stat().st_size for path in files), default=0)

    peak = 0.0
    for (t0, b0), (t1, b1) in zip(samples, samples[1:]):
        if t1 > t0:
            peak = max(peak, (b1 - b0) / (t1 - t0))
    download_s = (finished_at - started) if finished_at else total_s
    post_s = max(0.0, total_s - download_s)
    return {
        "n": fragments,
        "ok": not failure,
        "failure": failure,
        "size_bytes": size,
        "total_s": round(total_s, 1),
        "download_s": round(download_s, 1),
        "postprocess_s": round(post_s, 1),
        "avg_mbps": round(size / download_s / 1e6, 2) if download_s > 0 and size else 0.0,
        "peak_mbps": round(peak / 1e6, 2),
        "avg_megabits": safe_mbps(size, download_s),
        "peak_megabits": safe_mbps(int(peak), 1.0) if peak > 0 else 0.0,
        "hook_errors": errors,
    }


async def main() -> int:
    force_utf8_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--quality", default="720")
    parser.add_argument("--ns", nargs="+", type=int, default=[1, 2, 4, 8])
    args = parser.parse_args()

    base = Path(tempfile.mkdtemp(prefix="bench-frag-"))
    try:
        print(f"probing {args.url} ...", flush=True)
        try:
            probe_s, rungs = await _probe(args.url, base / "probe")
        except Exception as exc:  # noqa: BLE001 — reachability is the finding
            detail = str(exc).encode("ascii", "backslashreplace").decode("ascii")
            print(f"PROBE FAILED: {type(exc).__name__}: {detail}")
            print("No playable streams from this host (bot-check/PO-token) —")
            print("rerun on the production host or with a valid jar/tunnel.")
            return 2
        print(f"T1 probe: {probe_s:.1f}s, {rungs} video rungs\n", flush=True)

        results = []
        for n in args.ns:
            print(f"N={n} downloading ...", flush=True)
            row = await asyncio.to_thread(_run_once, args.url, args.quality, n, base)
            results.append(row)
            status = "OK " if row["ok"] else "FAIL"
            print(
                f"N={n}: {status} total={row['total_s']}s "
                f"dl={row['download_s']}s post={row['postprocess_s']}s "
                f"avg={row['avg_mbps']}MB/s peak={row['peak_mbps']}MB/s "
                f"size={row['size_bytes']}B errors={row['hook_errors']}"
                + (f" :: {row['failure']}" if row["failure"] else ""),
                flush=True,
            )
            shutil.rmtree(base / f"n{n}", ignore_errors=True)

        print("\nConfiguration    Avg MB/s    Total s    DL s    Post s    Failures")
        for row in results:
            print(
                f"N={row['n']:<13}{row['avg_mbps']:<12}{row['total_s']:<11}"
                f"{row['download_s']:<8}{row['postprocess_s']:<9}{row['hook_errors']}"
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = PROJECT_ROOT / f"bench_fragments-{stamp}.json"
        out.write_text(
            json.dumps(
                {
                    "url": args.url,
                    "quality": args.quality,
                    "t1_probe_s": round(probe_s, 1),
                    "video_rungs": rungs,
                    "runs": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")
        return 0 if all(row["ok"] for row in results) else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
