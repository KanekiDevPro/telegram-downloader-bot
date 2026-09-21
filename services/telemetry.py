"""Every failed download with its cause, and a weekly summary for the admins.

Single failures are already answered where they happen — the user gets the cause,
the admins get one notice. What no single failure can show is a *pattern*: thirty
login blocks in a week is one cookie re-export, thirty IP blocks is a proxy or a
token provider, and thirty "video unavailable" is nobody's fault. That pattern is
what this keeps, in a table the operator can also query by hand.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
from aiogram import Bot

from core import database
from core.config import get_settings
from core.utils import escape_html, utcnow
from services import helper_watch
from services.doctor import describe_age
from services.extractor import ExtractionError, classify_block, url_host
from services.queue import DownloadTask

logger = logging.getLogger(__name__)

#: Where the last digest was sent (in ``bot_state``), so the weekly gate survives
#: a restart instead of firing the report again on every deploy.
DIGEST_STATE_KEY = "block_digest_sent_at"
DIGEST_DAYS = 7
#: Telemetry older than this is pruned; it is a trend, not a ledger.
KEEP_DAYS = 90

#: The early alert: the weekly report is for the trend, this is the alarm. A jar
#: that stopped signing in fails *every* link, so waiting a week to mention it
#: costs a week of users — three login failures inside an hour is a pattern, and
#: the cooldown keeps a burst from paging more than twice an hour.
EARLY_ALERT_STATE_KEY = "block_early_alert_at"
EARLY_ALERT_THRESHOLD = 3
EARLY_ALERT_WINDOW_MINUTES = 60
EARLY_ALERT_COOLDOWN_S = 1800.0

#: ``/trend``'s window: long enough to see a fix's effect, short enough to fit one
#: Telegram message with a row per day that had failures.
TREND_DAYS = 14

#: How a cause reads inside a trend row (short — a row already has a date and a
#: total, and the rows have to scan quickly on a phone).
CAUSE_SHORT: dict[str, str] = {
    "login": "🔐لاگین",
    "ip": "🌐IP",
    "session": "🕰سشن",
    "site": "🎬سایت",
}

#: What a fix is called in the trend, by ``fix_events.kind``.
FIX_LABELS: dict[str, str] = {"cookie_jar": "جار کوکی"}

#: A verdict on a fix needs at least this much time on each side of it. Below it
#: the comparison is noise (ten quiet minutes after a fix is not evidence), and
#: saying "✅ 100% fewer" there would be exactly the kind of lie this project keeps
#: having to undo.
MIN_EFFECT_DAYS = 0.25  # six hours


@dataclass(frozen=True)
class TrendDay:
    """One local day of failures, per cause."""

    day: date
    counts: Mapping[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def count(self, cause: str) -> int:
        return self.counts.get(cause, 0)


@dataclass(frozen=True)
class FixMark:
    """A fix, when it happened, and where it landed on the calendar."""

    at: datetime
    kind: str
    detail: str
    local_day: date
    age_seconds: float

    @property
    def label(self) -> str:
        return FIX_LABELS.get(self.kind, self.kind)


@dataclass(frozen=True)
class FixEffect:
    """The same failures before and after a fix, as daily rates.

    Rates, not counts: the two windows are almost never the same length (a fix
    from yesterday has one day after it), and a raw count comparison would call
    every recent fix an improvement.
    """

    #: ``login`` (what the jar fix targets) or ``all`` (everything).
    cause: str
    label: str
    before: int
    after: int
    before_days: float
    after_days: float

    @property
    def before_rate(self) -> float:
        return self.before / max(self.before_days, 1e-9)

    @property
    def after_rate(self) -> float:
        return self.after / max(self.after_days, 1e-9)

    @property
    def change_percent(self) -> float | None:
        """How much the daily rate moved, or ``None`` when there was nothing to fix."""
        if self.before_rate <= 0:
            return None
        return (self.after_rate - self.before_rate) / self.before_rate * 100

    @property
    def verdict(self) -> str:
        """One honest word about the fix, from the two rates."""
        if self.before == 0:
            return "➖ قبل از اصلاح هم موردی از این نوع نبود"
        if self.after == 0:
            return "✅ کامل قطع شد"
        change = self.change_percent or 0.0
        if change <= -50:
            return "✅ محسوس کمتر شد"
        if change < 0:
            return "➖ کمی کمتر شد"
        if change == 0:
            return "➖ تغییری نکرد"
        return "⚠️ کمتر نشد — علت یا قدم بعدی چیز دیگری است"


@dataclass(frozen=True)
class Trend:
    """Failures per day, the last fix, and what that fix did to the failures."""

    days: int
    per_day: tuple[TrendDay, ...]
    overall: BlockDigest
    last_fix: FixMark | None
    effects: tuple[FixEffect, ...]
    too_soon: bool = False

    @property
    def quiet_days(self) -> int:
        return self.days - len(self.per_day)

#: How each cause reads in a report, in the order that matters to an operator.
CAUSE_LABELS: tuple[tuple[str, str], ...] = (
    ("login", "🔐 کوکی/لاگین"),
    ("ip", "🌐 IP یا PO token"),
    ("session", "🕰 سشن کهنه"),
    ("site", "🎬 خود سایت/لینک"),
)


@dataclass(frozen=True)
class BlockDigest:
    """Failures per cause over a window, plus the host that failed most."""

    #: How the window reads to a human ("۷ روز گذشته", "۶۰ دقیقهٔ گذشته").
    window: str
    counts: Mapping[str, int]
    top_host: tuple[str, int] | None

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def count(self, cause: str) -> int:
        return self.counts.get(cause, 0)


def next_step(digest: BlockDigest) -> str:
    """The single action the dominant cause deserves — or that none exists."""
    if digest.count("login"):
        return (
            "بیشترین علت لاگین است و از همین‌جا رفع می‌شود: یک اکسپورت تازه از کوکی "
            "(scripts/export_cookies.py) و بعد /doctor برای تأیید."
        )
    if digest.count("ip"):
        return (
            "بیشترین علت IP است: یک YTDLP_PROXY روی IP تمیز یا روشن‌کردن provider "
            "توکن، این را کم می‌کند."
        )
    if digest.count("session"):
        return "علت غالب سشن کهنه است — گذراست و ربات خودش با فاصله دوباره تلاش می‌کند."
    return "هیچ‌کدام از این شکست‌ها از سمت ربات قابل رفع نیست (لینک یا محتوای خود سایت)."


def render_digest(digest: BlockDigest, *, headline: str = "گزارش شکست‌ها") -> str:
    """The report as one Telegram message (the same shape for both reports)."""
    if digest.total == 0:
        return f"✅ در {digest.window} هیچ دانلودی با خطا ثبت نشده."
    lines = [f"📉 <b>{headline}</b>", "", f"کل: {digest.total} شکست در {digest.window}"]
    if digest.top_host is not None:
        host, count = digest.top_host
        lines.append(f"بیشترین میزبان: <code>{escape_html(host)}</code> ({count} مورد)")
    lines.append("")
    lines += [
        f"{label}: {digest.count(cause)}"
        for cause, label in CAUSE_LABELS
        if digest.count(cause)
    ]
    lines += ["", f"قدم بعدی: {next_step(digest)}"]
    return "\n".join(lines)


async def record_block(
    pool: asyncpg.Pool,
    task: DownloadTask,
    error: ExtractionError,
    cookie_file: Path | None,
) -> None:
    """Classify a failed download and store it.

    Never raises: telemetry must not be able to break the thing it measures, and a
    failed insert is not worth losing the user's error message over.
    """
    try:
        await database.record_block_event(
            pool,
            telegram_id=task.telegram_id,
            url_host=url_host(task.url),
            code=error.code,
            cause=classify_block(error, task.url, cookie_file),
        )
    except Exception:
        logger.exception("could not record the %s failure for %s", error.code, task.url)


async def build_digest(
    pool: asyncpg.Pool, *, days: int = DIGEST_DAYS, now: datetime | None = None
) -> BlockDigest:
    since = (now or utcnow()) - timedelta(days=days)
    return await _digest_since(pool, since, window=f"{days} روز گذشته")


async def build_recent_digest(
    pool: asyncpg.Pool,
    *,
    minutes: int = EARLY_ALERT_WINDOW_MINUTES,
    now: datetime | None = None,
) -> BlockDigest:
    since = (now or utcnow()) - timedelta(minutes=minutes)
    return await _digest_since(pool, since, window=f"{minutes} دقیقهٔ گذشته")


async def _digest_since(pool: asyncpg.Pool, since: datetime, *, window: str) -> BlockDigest:
    return BlockDigest(
        window=window,
        counts=await database.block_counts(pool, since),
        top_host=await database.top_block_host(pool, since),
    )


async def record_fix(pool: asyncpg.Pool, *, kind: str, detail: str = "") -> None:
    """Remember that something was fixed, so a trend can weigh it.

    Never raises, for the same reason as :func:`record_block`: the fix already
    happened, and failing to note it must not undo it.
    """
    try:
        await database.record_fix_event(pool, kind=kind, detail=detail)
    except Exception:
        logger.exception("could not record the %s fix", kind)


def _local_zone(name: str) -> tuple[tzinfo, str]:
    """The configured timezone plus the name Postgres resolves for the SQL.

    ``ZoneInfo`` needs a timezone database (the ``tzdata`` package on Windows), so
    a machine without one — or a typo'd ``TIMEZONE`` — falls back to UTC and says
    so, matching how the daily quotas already handle it. A report that cannot run
    is worse than a report in the wrong zone.
    """
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("TIMEZONE=%r is unknown here — the trend falls back to UTC", name)
        return timezone.utc, "UTC"


async def build_trend(
    pool: asyncpg.Pool,
    *,
    days: int = TREND_DAYS,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> Trend:
    """Failures per local day, plus what the last jar fix did to them.

    Days are grouped in the operator's timezone: "the day the fix landed" has to
    mean their day, or the before/after comparison is off by hours.
    """
    moment = now or utcnow()
    since = moment - timedelta(days=days)
    zone, zone_name = _local_zone(timezone_name or get_settings().timezone)

    grouped: dict[date, dict[str, int]] = {}
    for day, cause, count in await database.blocks_per_day(pool, since, zone_name):
        grouped.setdefault(day, {})[cause] = count
    per_day = tuple(
        TrendDay(day=day, counts=counts) for day, counts in sorted(grouped.items())
    )

    last_fix, effects, too_soon = await _fix_effect(pool, moment, since, zone)
    return Trend(
        days=days,
        per_day=per_day,
        overall=await _digest_since(pool, since, window=f"{days} روز گذشته"),
        last_fix=last_fix,
        effects=effects,
        too_soon=too_soon,
    )


async def _fix_effect(
    pool: asyncpg.Pool, moment: datetime, since: datetime, zone: tzinfo
) -> tuple[FixMark | None, tuple[FixEffect, ...], bool]:
    """The last jar fix, and the failures on either side of it."""
    latest = await database.latest_fix_event(pool, kind="cookie_jar")
    if latest is None:
        return None, (), False
    at, kind, detail = latest
    mark = FixMark(
        at=at,
        kind=kind,
        detail=detail,
        local_day=at.astimezone(zone).date(),
        age_seconds=max((moment - at).total_seconds(), 0.0),
    )

    after_days = max((moment - at).total_seconds(), 0.0) / 86_400
    available_before = max((at - since).total_seconds(), 0.0) / 86_400
    if after_days < MIN_EFFECT_DAYS or available_before < MIN_EFFECT_DAYS:
        # Either the fix is minutes old or it predates this window: there is
        # nothing to compare yet, and a quiet ten minutes is not a cure.
        return mark, (), True

    # Same-length window before the fix (clamped to what the trend can see) and
    # everything after it. Rates, not counts — the windows almost never match.
    before_days = min(available_before, max(after_days, MIN_EFFECT_DAYS))
    before = await database.block_counts(pool, at - timedelta(days=before_days), at)
    after = await database.block_counts(pool, at, moment)
    return (
        mark,
        (
            FixEffect(
                cause="login",
                label="🔐 کوکی/لاگین",
                before=before.get("login", 0),
                after=after.get("login", 0),
                before_days=before_days,
                after_days=after_days,
            ),
            FixEffect(
                cause="all",
                label="📊 همهٔ شکست‌ها",
                before=sum(before.values()),
                after=sum(after.values()),
                before_days=before_days,
                after_days=after_days,
            ),
        ),
        False,
    )


def _day_row(day: TrendDay) -> str:
    """One day, as the operator scans it: total first, then what it was made of."""
    if day.total == 0:
        return f"{day.day:%m-%d}  بدون شکست"
    broken = "، ".join(
        f"{CAUSE_SHORT.get(cause, cause)} {day.count(cause)}"
        for cause, _ in CAUSE_LABELS
        if day.count(cause)
    )
    return f"{day.day:%m-%d}  {day.total} شکست — {broken}"


def _fix_marker(fix: FixMark) -> str:
    detail = f": {escape_html(fix.detail)}" if fix.detail else ""
    return f"── 🔧 {fix.label}{detail} ──"


def _effect_line(effect: FixEffect) -> str:
    return (
        f"{effect.label}: {effect.before} → {effect.after} مورد "
        f"(روزی {effect.before_rate:.1f} → {effect.after_rate:.1f}) — {effect.verdict}"
    )


def render_trend(trend: Trend, *, headline: str | None = None) -> str:
    """The trend as one Telegram message: a row per day, the fix, and its effect."""
    title = headline or f"روند {trend.days} روزهٔ شکست‌ها"
    if trend.overall.total == 0 and trend.last_fix is None:
        return f"✅ در {trend.days} روز گذشته هیچ شکستی ثبت نشده."

    entries: list[tuple[date, int, str]] = [
        (day.day, 1, _day_row(day)) for day in trend.per_day
    ]
    if trend.last_fix is not None:
        # Sorted in, so it lands on its own day even when that day was quiet.
        entries.append((trend.last_fix.local_day, 0, _fix_marker(trend.last_fix)))
    entries.sort(key=lambda item: (item[0], item[1]))

    lines = [f"📈 <b>{title}</b>", ""]
    lines += [text for _, _, text in entries]
    if trend.quiet_days:
        lines.append(f"({trend.quiet_days} روز بدون شکست)")

    if trend.last_fix is not None:
        age = describe_age(trend.last_fix.age_seconds)
        lines.append("")
        lines.append(f"🔧 آخرین اصلاح: {age} — {trend.last_fix.label}")
        if trend.too_soon:
            lines.append(
                f"⏳ برای داوری زود است (کمتر از {MIN_EFFECT_DAYS * 24:.0f} ساعت از اصلاح) — "
                "بعداً دوباره /trend را بزنید."
            )
        else:
            lines += [_effect_line(effect) for effect in trend.effects]

    lines.append("")
    if trend.overall.total:
        lines.append(f"قدم بعدی: {next_step(trend.overall)}")
    else:
        lines.append("✅ در این بازه هیچ شکستی ثبت نشده — همین وضعیت را نگه دارید.")
    return "\n".join(lines)


async def maybe_send_digest(
    pool: asyncpg.Pool,
    bot: Bot,
    admin_ids: Iterable[int],
    *,
    days: int = DIGEST_DAYS,
    now: datetime | None = None,
) -> bool:
    """Send the weekly summary when a week has passed and there is something to say.

    A week with no failures stays silent *and changes nothing*: the stamp means "a
    report was delivered", so a quiet week — or a diagnostic run — cannot consume
    the window. It is called from the maintenance loop, which is why it must be
    harmless to call often (one count query) and must never spend the report on
    anything but an actual delivery.
    """
    admins = list(admin_ids)
    if not admins:
        return False
    moment = now or utcnow()
    if not await _due_after(pool, DIGEST_STATE_KEY, moment, days * 86_400):
        return False

    digest = await build_digest(pool, days=days, now=moment)
    # An outage of a helper is a failure too — often the reason a *later* week looks
    # worse — and it can happen without a single failed download (the fallback
    # covered them). So the report goes out when either of the two has something to
    # say, and stays silent only when both are quiet.
    outages = await helper_watch.summary(pool, days=days, now=moment)
    if digest.total == 0 and not outages:
        # Nothing to say, so nothing is spent: the next failure still gets a full
        # week's worth of report waiting for it.
        logger.debug("block digest: nothing failed in %s days", days)
        return False

    text = render_digest(digest, headline=f"گزارش {days} روزهٔ شکست‌ها")
    text = f"{text}\n\n{helper_watch.render_summary(outages)}"
    sent = await _send_to_admins(bot, admins, text)
    if sent:
        # Stamped only on delivery: if every send failed, next hour tries again.
        await _stamp(pool, DIGEST_STATE_KEY, moment)
        logger.info("block digest sent to %s admin(s): %s failures", sent, digest.total)
    else:
        logger.warning("block digest reached no admin — check ADMIN_IDS")
    return bool(sent)


async def maybe_send_early_alert(
    pool: asyncpg.Pool,
    bot: Bot,
    admin_ids: Iterable[int],
    *,
    now: datetime | None = None,
    threshold: int = EARLY_ALERT_THRESHOLD,
    cooldown_s: float = EARLY_ALERT_COOLDOWN_S,
) -> bool:
    """Page the admins when the same cause keeps failing inside an hour.

    Called after every recorded failure, so it must be cheap and quiet: one count
    query per failure, and a message only when a threshold is crossed, at most once
    per cooldown. This is what turns "a broken jar" into minutes instead of a week.
    """
    admins = list(admin_ids)
    if not admins:
        return False
    moment = now or utcnow()
    digest = await build_recent_digest(pool, now=moment)
    if digest.count("login") < threshold:
        return False
    if not await _due_after(pool, EARLY_ALERT_STATE_KEY, moment, cooldown_s):
        logger.info(
            "early alert suppressed: %s login failures in %s minutes but the last page "
            "was less than %.0fs ago",
            digest.count("login"),
            EARLY_ALERT_WINDOW_MINUTES,
            cooldown_s,
        )
        return False

    headline = f"🚨 {digest.count('login')} شکست لاگین در {EARLY_ALERT_WINDOW_MINUTES} دقیقهٔ اخیر"
    sent = await _send_to_admins(bot, admins, render_digest(digest, headline=headline))
    if sent:
        await _stamp(pool, EARLY_ALERT_STATE_KEY, moment)
        logger.warning(
            "early block alert sent to %s admin(s): %s login failures in %s minutes",
            sent,
            digest.count("login"),
            EARLY_ALERT_WINDOW_MINUTES,
        )
    return bool(sent)


async def _send_to_admins(bot: Bot, admin_ids: Iterable[int], text: str) -> int:
    """One message per admin; a blocked admin must not silence the others."""
    sent = 0
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
            sent += 1
        except Exception:
            logger.exception("could not send a block report to admin %s", admin_id)
    return sent


async def _due_after(
    pool: asyncpg.Pool, key: str, now: datetime, seconds: float
) -> bool:
    """Whether ``seconds`` have passed since ``key`` was last stamped."""
    last = await database.get_state(pool, key)
    if last is None:
        return True
    try:
        return (now - datetime.fromisoformat(last)).total_seconds() >= seconds
    except ValueError:
        logger.warning("bot_state[%s]=%r is not a timestamp — treating it as due", key, last)
        return True


async def _stamp(pool: asyncpg.Pool, key: str, now: datetime) -> None:
    try:
        await database.set_state(pool, key, now.isoformat())
    except Exception:
        logger.exception("could not record when %s was sent", key)
