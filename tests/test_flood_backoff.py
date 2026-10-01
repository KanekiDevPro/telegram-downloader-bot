"""Telegram flood-control backoff (P2-2): honor the server delay, boundedly.

One helper (:func:`services.delivery.flood_aware_call`) owns every flood wait:
delivery sends sleep the server's delay plus bounded jitter inside caps,
card edits get one small retry, cosmetics never sleep. Only
``TelegramRetryAfter`` is handled - permanent errors, network ambiguity and
``CancelledError`` all propagate. Sleeper and jitter are injected (no real
sleeping, no real network); the clock is never read by the helper itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from services.delivery import (
    FLOOD_EDIT_WAIT_MAX_S,
    FLOOD_JITTER_MAX_S,
    FLOOD_MAX_RETRIES,
    FLOOD_PER_WAIT_MAX_S,
    flood_aware_call,
)
from tests.test_quota_refund import (
    CLAIM,
    FROZEN_DAY,
    PRE_JOB,
    REFUND,
    _QuotaPool,
)
from tests.test_quota_refund import (
    _Bot as _QuotaBot,
)
from tests.test_quota_refund import (
    _Extractor as _QuotaExtractor,
)
from tests.test_quota_refund import (
    _run as _run_job,
)


def _flood(retry_after: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=None, message="flood", retry_after=retry_after)  # type: ignore[arg-type]


class _Script:
    """A send that fails on cue, then answers - recording every attempt."""

    def __init__(self, *effects: Any) -> None:
        self._effects = list(effects)
        self.calls = 0
        self.sleeps: list[float] = []

    async def send(self) -> str:
        self.calls += 1
        effect = self._effects.pop(0) if self._effects else "ok"
        if isinstance(effect, Exception):
            raise effect
        return effect

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)


async def test_delivery_honors_the_server_delay_plus_jitter() -> None:
    script = _Script(_flood(9), "sent")

    result = await flood_aware_call(
        script.send, kind="deliver", sleep=script.sleep, jitter=lambda: 0.25
    )

    assert result == "sent"
    assert script.calls == 2
    assert script.sleeps == [9.25]


async def test_jitter_is_bounded_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    def _record(high: float) -> float:
        seen.append(high)
        return 0.0

    monkeypatch.setattr(
        "services.delivery.random.uniform", lambda low, high: _record(high)
    )
    script = _Script(_flood(3), "sent")

    await flood_aware_call(script.send, kind="deliver", sleep=script.sleep)

    assert seen == [FLOOD_JITTER_MAX_S]
    assert script.sleeps == [3.0]


async def test_two_retries_then_the_flood_error_escapes() -> None:
    script = _Script(_flood(2), _flood(2), _flood(2), "never")

    with pytest.raises(TelegramRetryAfter):
        await flood_aware_call(
            script.send, kind="deliver", sleep=script.sleep, jitter=lambda: 0.0
        )

    assert script.calls == 1 + FLOOD_MAX_RETRIES
    assert script.sleeps == [2.0, 2.0]


async def test_an_over_cap_wait_raises_without_sleeping() -> None:
    script = _Script(_flood(int(FLOOD_PER_WAIT_MAX_S) + 1), "never")

    with pytest.raises(TelegramRetryAfter):
        await flood_aware_call(
            script.send, kind="deliver", sleep=script.sleep, jitter=lambda: 0.0
        )

    assert script.calls == 1
    assert script.sleeps == []


async def test_the_total_cap_binds_jitter_overflow() -> None:
    script = _Script(_flood(30), _flood(30), "never")

    with pytest.raises(TelegramRetryAfter):
        await flood_aware_call(
            script.send, kind="deliver", sleep=script.sleep, jitter=lambda: 1.0
        )

    # 30+1 spent, then 30 more would pass 60: the second wait never happens.
    assert script.calls == 2
    assert script.sleeps == [31.0]


async def test_permanent_errors_are_never_retried() -> None:
    for error in (
        TelegramBadRequest(method=None, message="bad"),  # type: ignore[arg-type]
        TelegramForbiddenError(method=None, message="blocked"),  # type: ignore[arg-type]
        RuntimeError("the network did something ambiguous"),
    ):
        script = _Script(error, "never")

        with pytest.raises(type(error)):
            await flood_aware_call(
                script.send, kind="deliver", sleep=script.sleep, jitter=lambda: 0.0
            )

        assert script.calls == 1
        assert script.sleeps == []


async def test_cancellation_during_the_wait_propagates() -> None:
    async def _cancelled(_delay: float) -> None:
        raise asyncio.CancelledError

    script = _Script(_flood(20), "never")

    with pytest.raises(asyncio.CancelledError):
        await flood_aware_call(
            script.send, kind="deliver", sleep=_cancelled, jitter=lambda: 0.0
        )

    assert script.calls == 1


async def test_a_set_stop_event_skips_every_wait() -> None:
    stop = asyncio.Event()
    stop.set()

    kinds: list[tuple[Literal["deliver", "edit", "cosmetic"], bool]] = [
        ("deliver", True),
        ("edit", False),
        ("cosmetic", False),
    ]
    for kind, raises in kinds:
        script = _Script(_flood(30), "never")
        if raises:
            with pytest.raises(TelegramRetryAfter):
                await flood_aware_call(script.send, kind=kind, stop_event=stop)
        else:
            assert await flood_aware_call(script.send, kind=kind, stop_event=stop) is None
        assert script.sleeps == [], kind
        assert script.calls == 1, kind


async def test_card_edits_retry_once_below_the_small_wait() -> None:
    script = _Script(_flood(int(FLOOD_EDIT_WAIT_MAX_S)), "edited")

    assert await flood_aware_call(
        script.send, kind="edit", sleep=script.sleep, jitter=lambda: 0.0
    ) == "edited"
    assert script.sleeps == [float(FLOOD_EDIT_WAIT_MAX_S)]


async def test_card_edits_give_up_above_the_small_wait() -> None:
    script = _Script(_flood(int(FLOOD_EDIT_WAIT_MAX_S) + 1), "never")

    assert await flood_aware_call(
        script.send, kind="edit", sleep=script.sleep, jitter=lambda: 0.0
    ) is None
    assert script.sleeps == []


async def test_card_edits_give_up_after_one_retry() -> None:
    script = _Script(_flood(1), _flood(1), "never")

    assert await flood_aware_call(
        script.send, kind="edit", sleep=script.sleep, jitter=lambda: 0.0
    ) is None
    assert script.calls == 2
    assert script.sleeps == [1.0]


async def test_cosmetics_never_sleep_and_report_the_delay() -> None:
    heard: list[float] = []
    script = _Script(_flood(30), "never")

    assert await flood_aware_call(
        script.send, kind="cosmetic", sleep=script.sleep, on_flood=heard.append
    ) is None
    assert script.calls == 1
    assert script.sleeps == []
    assert heard == [30.0]


async def test_an_unknown_kind_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        await flood_aware_call(_Script("x").send, kind="whatever")  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# Step C: the wiring around the helper (hazards a to f).
#
# Every flood below carries ``retry_after=0`` with the jitter pinned to zero,
# so the *real* helper runs end to end while the clock never advances: the
# waits are ``sleep(0.0)`` yields, never real sleeping, and the delay values
# themselves are already pinned above with injected sleeper fakes.
# ---------------------------------------------------------------------------


@pytest.fixture
def _still_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the helper's jitter to zero for the wiring tests below."""
    from types import SimpleNamespace

    import services.delivery as delivery_module

    monkeypatch.setattr(
        delivery_module, "random", SimpleNamespace(uniform=lambda low, high: 0.0)
    )


def _cache_row(**fields: Any) -> dict[str, Any]:
    """A replayable cache row: one stored file, one remembered kind."""
    return {
        "url_hash": "h",
        "telegram_file_id": "AgAC-one",
        "quality": "video",
        "original_url": "https://youtu.be/abc",
        "title": "A Clip",
        **fields,
    }


class _ReplayBot:
    """A bot that floods on cue, then answers — recording every attempt."""

    def __init__(self, *effects: Any) -> None:
        self._effects = list(effects)
        self.methods: list[str] = []

    def _answer(self, method: str) -> Any:
        from types import SimpleNamespace

        self.methods.append(method)
        effect = self._effects.pop(0) if self._effects else "ok"
        if isinstance(effect, Exception):
            raise effect
        return SimpleNamespace(video=SimpleNamespace(file_id="file-1"))

    async def send_video(self, chat_id: int, video: Any, **kwargs: Any) -> Any:
        return self._answer("send_video")

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        return self._answer("send_document")

    async def send_media_group(self, chat_id: int, media: Any, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        self.methods.append("send_media_group")
        effect = self._effects.pop(0) if self._effects else "ok"
        if isinstance(effect, Exception):
            raise effect
        return [SimpleNamespace(message_id=1) for _ in media]


async def test_a_flooded_replay_neither_downgrades_nor_forgets(
    _still_clock: None,
) -> None:
    """Hazard (a): a flood during replay is honoured *inside* the kind's send,
    so the row replays as what it is — never as a document, never dropped.

    And hazard (f): the retry lands only because the server took nothing.
    """
    from services import delivery

    bot = _ReplayBot(_flood(0), "ok")

    assert await delivery.send_cached_file(bot, 1, _cache_row(kind="video"), caption="c")  # type: ignore[arg-type]

    assert bot.methods == ["send_video", "send_video"], "same kind, retried — no downgrade"


async def test_a_relentless_flood_raises_retryable_instead_of_forgetting(
    _still_clock: None,
) -> None:
    """Hazard (a), the other half: when every retry floods, the error escapes
    into the attempt layer (retryable) instead of becoming ``False`` — and
    ``False`` is the only thing that forgets a cache row."""
    from services import delivery

    bot = _ReplayBot(_flood(0), _flood(0), _flood(0), _flood(0))

    with pytest.raises(TelegramRetryAfter):
        await delivery.send_cached_file(bot, 1, _cache_row(kind="video"), caption="c")  # type: ignore[arg-type]

    assert bot.methods == ["send_video"] * (1 + FLOOD_MAX_RETRIES)
    assert "send_document" not in bot.methods, "a flood is not a type mismatch"


async def test_an_album_retries_as_a_whole_or_not_at_all(
    _still_clock: None,
) -> None:
    """Hazard (d): Telegram rejects the whole media-group call on flood, so
    the whole batch retries — the batch is rebuilt, never subsetted."""
    from aiogram.types import InputMediaPhoto

    from services import delivery

    bot = _ReplayBot(_flood(0), "ok")
    photos = [InputMediaPhoto(media=f"id-{index}") for index in range(3)]
    seen: list[list[str]] = []
    real_group = bot.send_media_group

    async def _watch(chat_id: int, media: Any, **kwargs: Any) -> Any:
        seen.append([item.media for item in media])
        return await real_group(chat_id, media, **kwargs)

    bot.send_media_group = _watch  # type: ignore[method-assign]

    await delivery.send_album(bot, 1, photos, "caption")  # type: ignore[arg-type]

    assert seen == [["id-0", "id-1", "id-2"]] * 2, "both attempts carry the whole batch"


async def test_a_retried_upload_builds_a_fresh_input_object(
    _still_clock: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hazard (c), streamed leg: every retry builds its own ``FSInputFile`` —
    a partially consumed stream is never re-sent."""
    from aiogram.types import FSInputFile

    from services import worker

    async def _no_recover() -> None:
        return None

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 64)
    seen: list[Any] = []

    async def _send(ref: Any) -> str:
        seen.append(ref)
        if len(seen) == 1:
            raise _flood(0)
        return "sent"

    monkeypatch.setattr(worker, "_recover_local_api_if_healthy", _no_recover)
    monkeypatch.setattr(worker, "_media_ref", lambda path: FSInputFile(path))

    assert await worker._deliver_ref(_send, clip) == "sent"

    assert len(seen) == 2
    assert all(isinstance(ref, FSInputFile) for ref in seen)
    assert seen[0] is not seen[1], "the retry uploads a new object, not the consumed one"


async def test_a_retried_uri_upload_reuses_the_zero_copy_ref(
    _still_clock: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hazard (c), URI leg: a ``file://`` ref is a string, not a stream — the
    retry re-sends the same zero-copy reference (and the BadRequest fallback
    to streamed bytes still applies only to BadRequest, never to flood)."""
    from services import worker

    async def _no_recover() -> None:
        return None

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 64)
    seen: list[Any] = []

    async def _send(ref: Any) -> str:
        seen.append(ref)
        if len(seen) == 1:
            raise _flood(0)
        return "sent"

    monkeypatch.setattr(worker, "_recover_local_api_if_healthy", _no_recover)
    monkeypatch.setattr(worker, "_media_ref", lambda path: "file:///mnt/clip.mp4")

    assert await worker._deliver_ref(_send, clip) == "sent"

    assert seen == ["file:///mnt/clip.mp4"] * 2


async def test_a_flooded_progress_edit_skips_and_pushes_the_throttle(
    _still_clock: None,
) -> None:
    """Cosmetic end to end: the progress edit never sleeps and never retries —
    one attempt — and the next tick waits out the server's delay instead."""
    import time

    from services import worker

    calls: list[str] = []

    class _FloodingStatus:
        async def edit_text(self, text: str, **kwargs: Any) -> None:
            calls.append(text)
            raise _flood(7)

    editor = worker._ProgressEditor(_FloodingStatus(), "en")
    before = time.monotonic()

    await editor._edit("50%")

    assert len(calls) == 1, "cosmetics try once — the next throttled tick proceeds"
    assert editor._last_edit >= before + 7, "the throttle moved past the server's delay"


# ---------------------------------------------------------------------------
# Hazard (b): quota across a flooded job, through the real retry wrapper.
#
# The quota harness is borrowed from the refund tests (the same in-memory pool
# and job driver), with a bot that floods uploads on cue first: flood retries
# live *inside* one attempt, so one claim must cover them all.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _frozen_quota_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim clock and the pool's own clock read the same pinned day."""
    from core import database
    from services import worker

    monkeypatch.setattr(worker, "today_local", lambda: FROZEN_DAY)
    monkeypatch.setattr(database, "today_local", lambda: FROZEN_DAY)


class _FloodingBot(_QuotaBot):
    """The quota-harness bot, flooding uploads on cue before answering."""

    def __init__(self, floods: int) -> None:
        super().__init__()
        self._floods = floods
        self.attempts = 0

    def _send(self, kind: str) -> Any:
        self.attempts += 1
        if self._floods > 0:
            self._floods -= 1
            raise _flood(0)
        return super()._send(kind)


async def test_flood_retries_ride_on_the_one_claimed_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _still_clock: None
) -> None:
    """Hazards (b) and (f) together: floods absorbed inside the helper never
    touch quota — one claim for the attempt, no refund on success — and the
    user receives exactly one delivery for one download."""
    pool = _QuotaPool(today=FROZEN_DAY)
    extractor = _QuotaExtractor(tmp_path)
    bot = _FloodingBot(floods=2)

    await _run_job(monkeypatch, pool, bot, extractor)

    assert extractor.downloads == 1, "the flood never re-ran the download"
    assert bot.attempts == 1 + 2, "two floods honoured inside the one attempt"
    assert bot.uploads == ["video"], "exactly one delivery reached the chat"
    assert pool.count(CLAIM) == 1, "the attempt claimed once, retries claimed nothing"
    assert pool.count(REFUND) == 0, "success keeps the slot spent"


async def test_a_relentless_flood_refunds_exactly_once_per_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _still_clock: None
) -> None:
    """Hazard (b), the other half: when floods outlast every retry, each
    attempt refunds exactly once — and with the server taking nothing, no
    delivery exists to duplicate."""
    pool = _QuotaPool(today=FROZEN_DAY)
    extractor = _QuotaExtractor(tmp_path)
    bot = _FloodingBot(floods=10**6)

    await _run_job(monkeypatch, pool, bot, extractor)

    assert extractor.downloads == 3, "the retry wrapper really ran all its attempts"
    assert pool.count(CLAIM) == 3, "three attempts, three claims"
    assert pool.count(REFUND) == 3, "and every one of them comes back — once each"
    assert pool.daily == PRE_JOB, "the counter is back to its pre-job value"
    assert bot.uploads == [], "the server took nothing, so nothing was delivered"
