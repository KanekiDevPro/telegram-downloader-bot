"""Before the wait: does this link stand a chance?

A YouTube link handed to a bot whose jar cannot sign in usually comes back as
"Sign in to confirm you're not a bot" — after the queue, after extraction, and
after the user has waited. Two cheap facts settle it, and neither needs the
network: whether the jar can sign in at all, and whether anonymous requests were
actually refused *recently* (the watcher's probe and a failed download both record
that). When both say no, saying so before the link is queued is the honest thing —
the difference between a shrug after a wait and an explanation before one.

Nothing here blocks a link the jar *cannot* speak about: an unproven suspicion is
a warning, and only observed evidence turns it into a refusal.

One thing outranks all of it: a link the *fallback engine* can still serve must not
be refused here. Refusing it would be true about yt-dlp and false about the user's
file — the fallback exists exactly for the case this file refuses on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from services.extractor import (
    cookie_jar_is_usable,
    is_youtube_url,
    missing_youtube_login_cookies,
)

logger = logging.getLogger(__name__)

#: How long an observed refusal speaks for the present. Long enough to cover a
#: burst of links, short enough that a fix is believed: the jar's own login state
#: is still consulted on every call.
EVIDENCE_TTL_S = 900.0

#: Queued anyway, with the odds named. Most YouTube videos work anonymously, so
#: refusing on suspicion alone would be wrong.
RISKY_MESSAGE = (
    "ℹ️ نکته: کوکی ربات الان لاگین یوتیوب نیست، پس اگر این ویدیو لاگین لازم داشته باشد "
    "دانلود ممکن است شکست بخورد — در آن صورت به ادمین گزارش می‌شود."
)

#: Same suspicion, but the link has somewhere else to go (see COBALT_API_URL).
RISKY_FALLBACK_MESSAGE = (
    "ℹ️ نکته: کوکی ربات الان لاگین یوتیوب نیست، ولی اگر درخواست رد شود از مسیر جایگزین "
    "دانلود می‌شود — و اگر آن هم نشد، به ادمین گزارش می‌شود."
)

#: Not queued: YouTube has already refused an anonymous request on this host
#: minutes ago, and the cause is known and being worked on.
BLOCKED_MESSAGE = (
    "🚧 این لینک یوتیوب همین حالا دانلود نمی‌شود.\n\n"
    "آخرین تلاش‌ها نشان داده یوتیوب درخواست‌های فعلی ربات را ناشناس می‌بیند و رد می‌کند؛ "
    "علتش هم پیدا شده (لاگین نبودن کوکی) و به ادمین گزارش شده است.\n"
    "لطفاً کمی بعد دوباره بفرست — لینک‌های غیر یوتیوب مشکلی ندارند."
)

#: Queued: the refusal is real, but it is about the primary engine only — the
#: fallback is configured, so the file may well arrive anyway.
FALLBACK_QUEUE_MESSAGE = (
    "ℹ️ نکته: یوتیوب درخواست‌های ناشناس ربات را رد می‌کند؛ اگر لازم شود همین لینک از "
    "مسیر جایگزین دانلود می‌شود."
)

#: When an anonymous YouTube request was last refused (``monotonic`` seconds).
#: Process-local on purpose: it is a belief about "now", not a fact to persist.
_anonymous_refusal_at: float | None = None


@dataclass(frozen=True)
class Preflight:
    """What the gateway can say about a link before spending the user's wait.

    ``message`` is empty only for ``ok``, which keeps a refusal impossible to
    make without its explanation.
    """

    status: Literal["ok", "risky", "blocked"]
    message: str = ""

    @property
    def refused(self) -> bool:
        """``True`` when the link must not be queued until the jar is fixed."""
        return self.status == "blocked"


OK = Preflight("ok")


def note_anonymous_refusal(*, now: float | None = None) -> None:
    """Record that an anonymous request was just refused — that is the evidence."""
    global _anonymous_refusal_at
    _anonymous_refusal_at = time.monotonic() if now is None else now
    logger.debug("recorded an anonymous refusal as evidence")


def clear_anonymous_refusal() -> None:
    """Forget it: a request with cookies worked, or the jar learned to sign in."""
    global _anonymous_refusal_at
    _anonymous_refusal_at = None


def refusal_age_s(*, now: float | None = None) -> float | None:
    """How long ago an anonymous refusal was observed, or ``None`` if never."""
    if _anonymous_refusal_at is None:
        return None
    moment = time.monotonic() if now is None else now
    return moment - _anonymous_refusal_at


def refusal_is_recent(*, now: float | None = None) -> bool:
    age = refusal_age_s(now=now)
    return age is not None and age <= EVIDENCE_TTL_S


def youtube_preflight(
    url: str,
    cookie_file: Path | None,
    *,
    now: float | None = None,
    fallback_available: bool = False,
) -> Preflight:
    """Whether this link can work, from the jar's state and recent evidence.

    ``now`` exists so a caller (and a test) can judge the evidence against a
    chosen moment instead of the process clock. ``fallback_available`` means another
    engine is configured and may serve the link: it turns a refusal into a warning,
    because refusing a link the fallback can fetch would be telling the user "no"
    when the answer is "yes, a different way". Configured is not the same as
    verified — asking the instance on every link would cost more than it saves, and
    if it turns out to be unreachable the download fails as it would have anyway.
    """
    if not is_youtube_url(url):
        return OK
    if cookie_jar_is_usable(cookie_file) and not missing_youtube_login_cookies(cookie_file):
        # The jar signs in now: any refusal we remember was about the jar before it.
        clear_anonymous_refusal()
        return OK
    if refusal_is_recent(now=now):
        if fallback_available:
            return Preflight("risky", FALLBACK_QUEUE_MESSAGE)
        return Preflight("blocked", BLOCKED_MESSAGE)
    return Preflight("risky", RISKY_FALLBACK_MESSAGE if fallback_available else RISKY_MESSAGE)

