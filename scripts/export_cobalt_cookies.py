"""Write the fallback engine's cookie file from the jar the bot already has.

One export, both engines: ``cookies.txt`` is what yt-dlp reads, and this converts
it into the shape cobalt reads (``cookies.json``: service → ``Cookie:`` header
strings) in the directory the compose file mounts into the cobalt service.

The bot does this by itself — at startup and on every fresh export the watcher
sees — so this script is for the two cases the automation cannot cover: checking
what is on disk, and writing it from a machine where the bot is not running.

Usage:
    python scripts/export_cobalt_cookies.py                  # convert + write
    python scripts/export_cobalt_cookies.py --check          # report only
    python scripts/export_cobalt_cookies.py --jar other.txt --dir cobalt

Cobalt reads the file **once, at startup**: after a new export, the instance needs
``docker compose restart cobalt``. The script says so, and ``/doctor`` tells you
whether the running instance is on the current version.

Exit codes: 0 the file is current, 2 nothing was written (no jar, no YouTube
cookies, or a file that is not ours), 3 written but it carries no YouTube login.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_settings  # noqa: E402
from core.logging import force_utf8_console  # noqa: E402
from services import cobalt_cookies  # noqa: E402
from services.cobalt_cookies import CobaltCookieState  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--jar", type=Path, default=None, help="the Netscape jar to convert (default: COOKIE_FILE)"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="directory to write cookies.json into (default: COBALT_COOKIES_DIR)",
    )
    parser.add_argument(
        "--check", action="store_true", help="report what is on disk and write nothing"
    )
    return parser.parse_args(argv)


def report(state: CobaltCookieState, *, verbose: bool = True) -> int:
    """Print the facts, then the one thing that makes a new file live."""
    if state.off:
        print("COBALT_COOKIES_DIR is empty — nothing is generated, so the fallback runs")
        print("without cookies. Set it (./cobalt works) or leave the fallback without one.")
        return 2
    print(state.describe())
    if not state.usable:
        if state.cookie_count:
            # The jar is fine; the file simply has not been generated yet (a
            # `--check` before the first run). Say that, not "your jar is broken".
            print("\nrun this without --check to write it.")
        else:
            print(
                "\nnothing to write. Usual causes:\n"
                "  - no cookie jar yet: export one first (python scripts/export_cookies.py)\n"
                "  - a jar with no youtube.com cookies: log into YouTube in that browser\n"
                "  - a cookies.json that is not this deployment's (a hand-made file, or the\n"
                "    flat-array shape one of the cobalt docs' examples suggests) — it is left\n"
                "    alone on purpose"
            )
        return 2
    if state.other_services:
        print(f"kept other services in the file: {', '.join(state.other_services)}")
    if verbose:
        print("\n← cobalt reads this once, at startup: docker compose restart cobalt")
        print("  /doctor says whether the running instance has the current version.")
    if state.missing_login:
        print(
            f"\nWARNING: no YouTube login in this file ({', '.join(state.missing_login)} missing).\n"
            "  Cobalt still uses the cookies for its player/bot-check path (better than none),\n"
            "  but a signed-in export is what makes a flagged IP work: same advice as the jar —\n"
            "  log into YouTube in the browser you export from, and keep HttpOnly rows."
        )
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    settings = get_settings()

    jar = args.jar or settings.cookie_file
    if args.dir is not None:
        settings = settings.model_copy(update={"cobalt_cookies_dir": args.dir})
    elif settings.cobalt_cookies_dir is None and jar is not None:
        # `--jar` without `--dir` on a deployment that turned generation off: the
        # sensible reading is "convert this into the default directory".
        settings = settings.model_copy(update={"cobalt_cookies_dir": PROJECT_ROOT / "cobalt"})

    if args.check:
        return report(
            cobalt_cookies.read_state(settings, jar_path=jar), verbose=False
        )
    state = cobalt_cookies.sync_from_jar(settings, jar_path=jar)
    if state.usable and state.written:
        print(f"wrote {state.cookie_count} youtube cookie(s) from {jar} to {state.path}")
    elif state.usable:
        print(f"already current: {state.path} ({state.cookie_count} youtube cookies)")
    return report(state)


if __name__ == "__main__":
    raise SystemExit(main())
