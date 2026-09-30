"""Explicit per-user download-mode/session isolation.

When a user taps a platform section (Spotify, YouTube, …) the bot enters that
section's *mode*: while the mode is active only URLs compatible with it are
accepted, so a YouTube link can never slip into a Spotify session (or vice
versa) and a stale format callback from an earlier mode can never fire a
download for the current one.

The mode itself lives in the user's FSM data (``download_mode`` — per user,
never global), while the *lifecycle* (\"are relevant jobs still running?\")
lives here: a tiny per-process registry of active jobs, incremented by the
gateway when a task is enqueued and decremented by the worker when that task
settles. The gateway and the workers run in the same process (see ``main.py``),
so one registry serves both sides without a new channel.

Modes
-----

``NONE`` (no constraint), ``SPOTIFY``, ``YOUTUBE`` and ``GENERIC`` (the
Instagram/TikTok/other sections — everything that is neither Spotify nor
YouTube). A platform tap maps to exactly one of them; a URL maps to exactly
one of them; a URL is accepted only when the two agree.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

#: The FSM data key holding the user's current mode (see ``handlers/user.py``).
MODE_KEY = "download_mode"

#: No constraint — the ordinary behaviour.
NONE = "none"
#: The Spotify section is active: only Spotify URLs are accepted.
SPOTIFY = "spotify"
#: The YouTube section is active: only YouTube URLs are accepted.
YOUTUBE = "youtube"
#: An Instagram/TikTok/other section is active: Spotify and YouTube URLs are
#: rejected, everything else is accepted.
GENERIC = "generic"

DownloadMode = Literal["none", "spotify", "youtube", "generic"]

#: Every mode value, for validation of whatever the FSM hands back.
ALL_MODES: tuple[str, ...] = (NONE, SPOTIFY, YOUTUBE, GENERIC)


def mode_for_platform(name: str) -> str:
    """Which mode entering the ``name`` platform section activates."""
    if name == "spotify":
        return SPOTIFY
    if name == "youtube":
        return YOUTUBE
    return GENERIC


def mode_for_url(url: str) -> str:
    """Which mode a URL belongs to (``GENERIC`` covers instagram/tiktok/other)."""
    from services import content

    platform = content.platform_for(url)
    if platform == "spotify":
        return SPOTIFY
    if platform == "youtube":
        return YOUTUBE
    return GENERIC


def is_compatible(mode: str, url: str) -> bool:
    """Whether ``url`` may be accepted while ``mode`` is active."""
    if mode == NONE:
        return True
    return mode_for_url(url) == mode


def wrong_source_key(mode: str) -> str:
    """Catalogue key explaining a rejection under ``mode``."""
    if mode == SPOTIFY:
        return "download.wrong_source_spotify"
    if mode == YOUTUBE:
        return "download.wrong_source_youtube"
    return "download.wrong_source_generic"


def normalize(mode: object) -> str:
    """Whatever the FSM stored, back to a known mode (unknown → ``NONE``)."""
    return mode if mode in ALL_MODES else NONE


# ---------------------------------------------------------------------------
# Active-job lifecycle
# ---------------------------------------------------------------------------
#
# ``(telegram_id, mode) → running jobs``. Incremented by the gateway the moment
# a task is enqueued (``handlers/user.py::_submit``), decremented by the worker
# the moment that task settles (``services/worker.py::_settle``) — every
# settled outcome decrements, no requeue path does, so the count covers queued,
# downloading, processing, uploading and retrying work alike. A mode whose
# count is zero has no relevant work left and may unlock.

_ACTIVE: dict[tuple[int, str], int] = {}

#: ``(telegram_id, mode)`` pairs that have had at least one job since the
#: current session began. A freshly entered mode has no jobs *yet* — without
#: this, "unlock when no jobs remain" would fire the moment the section opens,
#: before the user sends a single link. Only a used session may unlock.


_SEEN: set[tuple[int, str]] = set()


def begin_session(telegram_id: int) -> None:
    """A new mode session starts: forget what earlier sessions ran.

    Called when the user enters a section. Switching sections is only allowed
    while the old mode has no active jobs, so dropping the old marks is safe —
    and required, or a *previous* session's jobs would unlock the new one.
    """
    for key in [key for key in _SEEN if key[0] == telegram_id]:
        _SEEN.discard(key)


def was_used(telegram_id: int, mode: str) -> bool:
    """Whether this session has run at least one job of ``mode``."""
    return (telegram_id, normalize(mode)) in _SEEN


def job_started(telegram_id: int, mode: str) -> int:
    """Record one more active job for this user and mode; returns the count."""
    key = (telegram_id, normalize(mode))
    _SEEN.add(key)
    _ACTIVE[key] = _ACTIVE.get(key, 0) + 1
    return _ACTIVE[key]


def job_settled(telegram_id: int, mode: str) -> int:
    """Record one finished job; returns the remaining count (never negative)."""
    key = (telegram_id, normalize(mode))
    remaining = _ACTIVE.get(key, 0) - 1
    if remaining <= 0:
        _ACTIVE.pop(key, None)
        return 0
    _ACTIVE[key] = remaining
    return remaining


def job_settled_for_url(telegram_id: int, url: str) -> int:
    """Settle one job by URL (the worker only carries the task, not a mode)."""
    return job_settled(telegram_id, mode_for_url(url))


def active_count(telegram_id: int, mode: str) -> int:
    """How many jobs of this user's mode are still running."""
    return _ACTIVE.get((telegram_id, normalize(mode)), 0)


def has_active(telegram_id: int, mode: str) -> bool:
    """Whether this user's mode still has work running (never unlockable)."""
    return active_count(telegram_id, mode) > 0


def reset_for_tests() -> None:
    """Empty the registry (tests only — production counts live and die here)."""
    _ACTIVE.clear()
    _SEEN.clear()
