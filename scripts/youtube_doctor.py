"""Print one verdict for the YouTube download path.

Runs the same checks as the admin-only ``/doctor`` command: cookie jar (and
whether it is actually logged in), JavaScript runtime, PO-token provider, ffmpeg,
optional proxy, then a live metadata probe.

Usage:
    python scripts/youtube_doctor.py                 # full check
    python scripts/youtube_doctor.py --no-probe      # offline, config only
    python scripts/youtube_doctor.py --url <link>    # probe another video

Exit codes: 0 healthy, 1 something is broken (the report says what to do).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_settings, probe_url  # noqa: E402
from core.logging import force_utf8_console, setup_logging  # noqa: E402
from services.cobalt import CobaltService  # noqa: E402
from services.doctor import DEFAULT_PROBE_URL, run_youtube_doctor  # noqa: E402
from services.extractor import ExtractorService  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--no-probe", action="store_true", help="skip the live YouTube request")
    parser.add_argument("--url", default=DEFAULT_PROBE_URL, help="URL to probe")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    args = parse_args(argv)
    settings = get_settings()
    # yt-dlp's own noise would bury the report; warnings still come through the logger.
    setup_logging("WARNING")

    extractor = ExtractorService(
        settings.download_dir,
        # Diagnose the first response, not the retried one: the report must say
        # why the *first* attempt failed, and the bot's own retry is reported
        # separately in the verdict.
        retry_attempts=0,
        timeout_s=settings.extractor_timeout_s,
        download_timeout_s=settings.download_timeout_s,
        cookie_file=settings.cookie_file,
        proxy=settings.ytdlp_proxy,
        # The reachable address, like the bot's own engine gets: this is a host
        # process, where a compose service name does not resolve — an extractor
        # here holding `pot-provider:4416` would probe *without* a token and blame
        # the link for it.
        pot_provider_url=probe_url(settings.ytdlp_pot_provider_url),
        cookies_from_browser=settings.cookies_from_browser,
        js_runtime=settings.ytdlp_js_runtime,
    )

    # The fallback engine gets a client of its own here: the CLI is not the bot, so
    # the doctor cannot borrow the running one — but leaving it out would hide the
    # single most common reason a blocked link still fails (an instance that wants
    # a key), which is exactly what an operator runs this script to find out.
    #
    # The address is translated for the same reason the bot translates it: this is
    # a host process, where a compose service name does not resolve — and a report
    # that says "unreachable" about a healthy instance is worse than no report.
    cobalt = CobaltService(
        [probe_url(url) for url in settings.cobalt_endpoints],
        api_key=settings.cobalt_api_key,
        timeout_s=settings.cobalt_timeout_s,
        download_timeout_s=settings.cobalt_download_timeout_s,
        proxy=settings.cobalt_proxy or None,
    )
    try:
        report = await run_youtube_doctor(
            settings, extractor, cobalt=cobalt, probe=not args.no_probe, probe_url=args.url
        )
    finally:
        await cobalt.close()
    print(report.render())
    if not report.healthy:
        print(
            "\n(این اسکریپت کاری را عوض نمیکند؛ فقط میگوید کدام قطعه خراب است. "
            "راهنمای کامل: README → «Sign in to confirm you're not a bot»)"
        )
    return 0 if report.healthy else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
