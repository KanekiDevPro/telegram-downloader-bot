"""Export a browser's cookie jar to ``cookies.txt`` for yt-dlp.

Run this on the machine that has the browser (your laptop), then copy the file
next to the bot (or into the container's build context). Extracting cookies from
*inside* a container usually cannot work: the profile belongs to another OS user
and Chromium's cookies are encrypted with that user's keyring.

Usage:
    python scripts/export_cookies.py                       # chrome, auto profile
    python scripts/export_cookies.py --browser firefox
    python scripts/export_cookies.py --browser chrome --profile "Profile 2"
    python scripts/export_cookies.py --browser chrome+gnomekeyring --profile Default

The output file contains account cookies: it is as sensitive as your password.
The bot's ``.gitignore`` already ignores ``cookies.txt``.

Exit codes: 0 written and usable, 2 nothing written, 3 written but it carries no
YouTube login (HTTP-only rows missing) — yt-dlp would then send anonymous
requests and YouTube answers "Sign in to confirm you're not a bot".
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.logging import force_utf8_console  # noqa: E402

# The export itself lives in the bot's service layer: the same code runs when the
# jar is refreshed automatically after a login-shaped block (COOKIE_AUTO_EXPORT).
from services.cookie_refresh import ExportError, export_jar_from_browser  # noqa: E402
from services.extractor import (  # noqa: E402
    BrowserSpecError,
    cookie_jar_is_usable,
    missing_youtube_login_cookies,
    read_netscape_cookie_names,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "cookies.txt"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--browser",
        default="chrome",
        help="BROWSER[+KEYRING], e.g. chrome, chrome+gnomekeyring, firefox, edge, brave",
    )
    parser.add_argument("--profile", default=None, help="profile name or path (default: most recent)")
    parser.add_argument("--container", default=None, help="Firefox container name (rarely needed)")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"where to write the jar (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument("--verbose", action="store_true", help="show yt-dlp's own diagnostics")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    # The service logs its progress (which profile it read, Docker placeholders it
    # removed) as plain lines, which is what this script printed itself before.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)

    spec = args.browser if args.profile is None else f"{args.browser}:{args.profile}"
    if args.container:
        spec = f"{spec}::{args.container}"

    try:
        count = export_jar_from_browser(spec, args.output, verbose=args.verbose)
    except BrowserSpecError as exc:
        print(f"invalid browser spec: {exc}")
        return 2
    except ImportError as exc:  # pragma: no cover — missing yt-dlp
        print(f"yt-dlp is not installed in this environment: {exc}")
        return 2
    except ExportError as exc:
        print(f"\n{exc}\n")
        print("nothing was written. Usual causes, most common first:")
        print("  - the browser is still running: Chromium locks its cookie database, close it fully")
        print("  - a different profile holds the login: pass --profile \"Profile 1\" (see")
        print("    chrome://version → Profile Path)")
        print("  - on Linux the keyring cannot be unlocked in this session: add +gnomekeyring or")
        print("    +kwallet to --browser")
        print('  - last resort: a "Get cookies.txt" style extension writes the same format')
        return 2

    usable = cookie_jar_is_usable(args.output)
    domains: Counter[str] = Counter()
    for line in args.output.read_text(encoding="utf-8", errors="replace").splitlines():
        row = line
        if row.startswith("#HttpOnly_"):  # HttpOnly rows hold the login cookies
            row = row[len("#HttpOnly_") :]
        elif row.startswith("#") or not row.strip():
            continue
        parts = row.split("\t")
        if len(parts) >= 7:
            domains[parts[0].lstrip(".")] += 1

    print(f"wrote {count} cookies to {args.output}")
    print(f"top domains: {', '.join(f'{d} ({n})' for d, n in domains.most_common(5))}")
    print("youtube cookies:", sum(n for d, n in domains.items() if "youtube" in d or "google" in d))
    if usable and "youtube" not in "".join(domains):
        print(
            "note: no youtube.com cookies in this jar — log into YouTube in that browser first "
            "(Google cookies alone are not enough)."
        )

    # A jar can load perfectly and still not be signed in: yt-dlp needs
    # LOGIN_INFO plus a SAPISID cookie, and those are the HttpOnly rows an
    # exporter is most likely to have skipped. Catching it here saves the
    # operator from debugging "Sign in to confirm you're not a bot" later.
    missing = missing_youtube_login_cookies(args.output)
    if missing:
        print(
            f"\nWARNING: this jar carries no YouTube login ({', '.join(missing)} missing).\n"
            "  yt-dlp will send anonymous requests, and YouTube answers 'Sign in to confirm\n"
            "  you're not a bot' for them on flagged IPs — which looks like a network problem\n"
            "  but is not. Check, in order:\n"
            "    - you were signed in to YouTube in that browser profile when exporting\n"
            "    - the exporter kept HttpOnly cookies (Chrome DevTools exports usually do,\n"
            "      'Get cookies.txt' extensions often do not)\n"
            "    - --profile points at the profile that is signed in (chrome://version)\n"
            "  Extracted cookie names:",
            ", ".join(sorted(read_netscape_cookie_names(args.output))[:8]) or "(none)",
        )
    print(
        "\nThis file grants access to your accounts — keep it out of git and off shared hosts.\n"
        "Next: keep it next to the bot as cookies.txt (COOKIE_FILE). In Docker it is mounted\n"
        "read-only and re-read on every download, so no rebuild and no restart are needed."
    )
    return 3 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
