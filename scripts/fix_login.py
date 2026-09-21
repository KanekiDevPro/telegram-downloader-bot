"""Guided wizard: get a *real* YouTube login into the cookie jar.

Run this on the machine that has the signed-in browser. It:

1. reads the current jar and says whether it can sign YouTube in at all
   (``LOGIN_INFO`` plus a ``SAPISID`` cookie) — an export that dropped the
   HTTP-only rows looks exactly like "no browser is signed in";
2. finds the browser profiles this machine has and reads one — atomically, and
   never overwriting a jar that *does* sign in;
3. checks the new jar for the login cookies *before* saying anything else: a login
   that did not land is the answer, not a detail;
4. probes YouTube live with the same cheap metadata request ``/doctor`` uses;
5. tells the admins the outcome in Telegram (so the steps you followed end in a
   confirmation instead of silence) and records the fix — which is what ``/trend``
   later weighs the failures against.

Usage:
    python scripts/fix_login.py                  # detect, choose a profile, replace, probe
    python scripts/fix_login.py --browser edge:Default
    python scripts/fix_login.py --dry-run        # check an export, change nothing
    python scripts/fix_login.py --no-notify      # don't message the admins

Exit codes: 0 the jar now signs in, 1 the login still is not there (with the
reason), 2 nothing was attempted (no profile to read, or dry-run said no).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.enums import ParseMode  # noqa: E402

from core.config import Settings, get_settings  # noqa: E402
from core.database import create_pool  # noqa: E402
from core.logging import force_utf8_console  # noqa: E402
from core.telegram_api import build_session  # noqa: E402
from services import cookie_refresh, login_wizard, telemetry  # noqa: E402
from services.doctor import http_reachable  # noqa: E402
from services.extractor import ExtractorService, missing_youtube_login_cookies  # noqa: E402

#: The probe is a diagnosis, not a download: a short timeout and one attempt.
PROBE_TIMEOUT_S = 45


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--browser",
        default=None,
        help="profile to read: BROWSER[+KEYRING][:PROFILE], e.g. edge:Default, chrome, firefox",
    )
    parser.add_argument(
        "--jar", type=Path, default=None, help="the cookie jar to fix (default: COOKIE_FILE)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="export and inspect, but leave the real jar untouched",
    )
    parser.add_argument("--yes", action="store_true", help="never prompt; take the first profile")
    parser.add_argument("--no-notify", action="store_true", help="do not message the admins")
    return parser.parse_args(argv)


async def _pot_provider(settings: Settings) -> str:
    """The provider URL only when it answers — a down provider fails *every* probe."""
    url = settings.ytdlp_pot_provider_url
    if not url or await http_reachable(url):
        return url
    print(f"  ⚠ PO token provider at {url} does not answer — probing without it")
    return ""


def build_extractor(settings: Settings, jar: Path, *, pot_provider_url: str = "") -> ExtractorService:
    """An extractor pointed at this jar: what the probe and diagnosis read."""
    return ExtractorService(
        settings.download_dir,
        timeout_s=PROBE_TIMEOUT_S,
        cookie_file=jar,
        proxy=settings.ytdlp_proxy,
        pot_provider_url=pot_provider_url,
        js_runtime=settings.ytdlp_js_runtime,
        retry_attempts=0,  # a diagnosis, not a download
    )


def choose_profile(
    profiles: tuple[login_wizard.ProfileCandidate, ...], *, assume_yes: bool
) -> login_wizard.ProfileCandidate | None:
    """Pick the profile to read: the only one, or ask, or the first with ``--yes``."""
    if not profiles:
        return None
    if assume_yes or len(profiles) == 1:
        return profiles[0]
    print("  کدام پروفایل خوانده شود؟")
    for index, profile in enumerate(profiles, start=1):
        print(f"    {index}) {profile.label}")
    try:
        answer = input("  شماره [1]: ").strip()
    except EOFError:  # not a terminal: the first one, and say so
        answer = ""
    if not answer:
        return profiles[0]
    if not answer.isdigit() or not 1 <= int(answer) <= len(profiles):
        print("  ✘ انتخاب نامعتبر بود.")
        return None
    return profiles[int(answer) - 1]


def print_traps() -> None:
    print("  دو تلهٔ همیشگی:")
    for trap in login_wizard.LOGIN_TRAPS:
        print(f"    • {trap}")


async def _tell_admins(settings: Settings, text: str) -> bool:
    """Message every admin; the point of the wizard is not to end at a prompt."""
    if not settings.admin_ids or not settings.bot_token:
        print("  ⚠ ADMIN_IDS/BOT_TOKEN نیست — نتیجه فقط همین‌جا چاپ شد")
        return False
    bot = Bot(
        settings.bot_token,
        session=build_session(settings),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    delivered = 0
    try:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text)
                delivered += 1
            except Exception as exc:  # noqa: BLE001 — one blocked admin, not the end
                print(f"  ⚠ ارسال به {admin_id} نشد: {exc}")
    finally:
        await bot.session.close()
    return delivered > 0


async def _record_fix(settings: Settings, detail: str) -> bool:
    """Record the fix for /trend; a database that is down is a warning, not a failure."""
    try:
        pool = await create_pool()
    except Exception as exc:  # noqa: BLE001 — the fix already happened
        print(f"  ⚠ ثبت در دیتابیس نشد ({exc}) — /trend این اصلاح را نمی‌بیند")
        return False
    try:
        await telemetry.record_fix(pool, kind="cookie_jar", detail=detail)
    finally:
        await pool.close()
    return True


def _dry_run_target(jar: Path) -> Path:
    """A throwaway copy of the jar, so a dry run can exercise the real code path."""
    scratch = Path(tempfile.mkdtemp(prefix="fix-login-"))
    target = scratch / jar.name
    shutil.copy(jar, target)
    return target


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    jar = args.jar or settings.cookie_file
    if jar is None:
        print("✘ COOKIE_FILE تنظیم نیست؛ بگو کدام فایل جار است (--jar).")
        return 2
    jar = Path(jar)

    print("== fix_login ==")
    print(f"  جار: {jar}")
    pot_url = await _pot_provider(settings)
    extractor = build_extractor(settings, jar, pot_provider_url=pot_url)
    diagnosis = login_wizard.diagnose(extractor)
    print(f"  وضعیت: {diagnosis.summary}")

    if diagnosis.signed_in:
        print("  ✔ لاگین یوتیوب کامل است — چیزی برای درست‌کردن نیست.")
        verdict = await login_wizard.probe(extractor)
        print(f"  {verdict}")
        return 0

    profiles = login_wizard.candidates()
    print(f"  پروفایل‌های این ماشین: {len(profiles)}")
    for profile in profiles:
        print(f"    • {profile.label}")

    spec = args.browser
    if spec is None and profiles:
        chosen = choose_profile(profiles, assume_yes=args.yes)
        spec = chosen.spec if chosen is not None else None
    if spec is None:
        print("  ✘ پروفایلی برای خواندن نیست (کانتینر/سرور؟).")
        for step in login_wizard.next_steps(diagnosis, have_profile=False):
            print(f"    • {step}")
        return 2

    target = _dry_run_target(jar) if args.dry_run else jar
    skipped = " (dry-run: جار واقعی دست‌نخورده)" if args.dry_run else ""
    print(f"  خواندن کوکی از {spec}…{skipped}")
    result = await cookie_refresh.import_from_profile(spec, target)
    if result.kind == "kept":
        print(f"  ✘ {result.message}")
        print_traps()
        return 1
    if result.kind != "replaced":
        print(f"  ✘ {result.message}")
        if not login_wizard.app_bound_encryption_active(spec):
            # The lock message already lists what works; the generic steps would
            # send the operator back to a browser that cannot be read at all.
            for step in login_wizard.next_steps(diagnosis, have_profile=bool(profiles)):
                print(f"    • {step}")
        return 2
    print(f"  ✔ اکسپورت شد ({result.cookies} کوکی){skipped}")

    missing = missing_youtube_login_cookies(target)
    if missing:
        # The login is the whole point: a jar without it changes nothing, and
        # saying "done" here is what makes this failure repeat.
        print(f"  ✘ لاگین یوتیوب در اکسپورت نیست — کم: {', '.join(missing)}")
        print_traps()
        return 1
    print("  ✔ لاگین یوتیوب رسید (LOGIN_INFO + SAPISID)")

    verdict = await login_wizard.probe(build_extractor(settings, target, pot_provider_url=pot_url))
    print(f"  {verdict}")

    if args.dry_run:
        print("  (dry-run) هیچ‌چیز جایگزین/ثبت/ارسال نشد.")
        return 0 if verdict.startswith("✅") else 1

    detail = f"دستی (fix_login): {spec} → {result.cookies} کوکی"
    await _record_fix(settings, detail)
    if not args.no_notify:
        await _tell_admins(
            settings,
            f"🔧 <b>اکسپورت دستی کوکی انجام شد</b> ({result.cookies} کوکی از {spec}) و "
            f"همین حالا تست شد:\n\n{verdict}",
        )
    return 0 if verdict.startswith("✅") else 1


def main(argv: list[str] | None = None) -> int:
    force_utf8_console()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
