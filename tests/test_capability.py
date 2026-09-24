"""The capability model: what a source can honestly become.

Every screen asks this one model — the audio format grid, the bitrate rows, the
size estimates — so the UI can never promise what the pipeline cannot deliver.
These tests are the contract behind four live symptoms: a FLAC tap whose file
captioned itself "MP3", a source offered a bitrate "upgrade" it could not use, a
SoundCloud FLAC that was really 57 MB of upload refusal dressed as an internal
error, and a video menu that collapsed a real format list into one «Download»
row. A button may only exist when the execution path can finish what it says.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge

from core.i18n import t
from handlers import user as user_module
from services import content
from services import worker as worker_module
from services.extractor import (
    DownloadResult,
    ExtractionError,
    MediaInfo,
    VideoOption,
    audio_capability,
    source_audio_ext,
)

FA = "fa"
EN = "en"
URL = "https://soundcloud.com/pooriaputak/helia-yadegari-3"

#: The live SoundCloud case, measured: 5:10 of lossless audio is ~57 MB.
SOUNDCLOUD_S = 310.578
CLOUD_LIMIT = 50 * 1024 * 1024
LOCAL_LIMIT = 2000 * 1024 * 1024


# ---------------------------------------------------------------------------
# The capability contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_ext", "expected"),
    [
        ("m4a", True),
        ("aac", True),
        ("m4b", True),
        ("mp4", False),
        ("webm", False),
        ("opus", False),
        ("mp3", False),
        (None, True),
    ],
)
def test_the_copy_row_is_only_offered_for_a_stream_that_is_m4a(
    source_ext: str | None, expected: bool
) -> None:
    """A no-conversion tier promises a container — honest only when it is one."""
    assert audio_capability(source_ext=source_ext, has_ffmpeg=True).copy_ok is expected


def test_without_ffmpeg_only_an_honest_copy_can_be_promised() -> None:
    """A re-encode needs the tool; a copy needs the right stream. Neither → nothing."""
    assert audio_capability(source_ext="m4a", has_ffmpeg=False).formats == ("m4a",)
    assert audio_capability(source_ext="webm", has_ffmpeg=False).formats == ()


def test_with_ffmpeg_every_output_codec_is_producible() -> None:
    capability = audio_capability(source_ext="webm", has_ffmpeg=True)
    assert capability.formats == ("mp3", "m4a", "flac", "opus", "wav")


def test_lossless_disappears_when_the_file_cannot_be_carried() -> None:
    """The live case: FLAC *is* producible here — but not deliverable, so it is
    not offered. An option the upload would only refuse is not an option."""
    capability = audio_capability(
        source_ext="m4a",
        duration_s=SOUNDCLOUD_S,
        has_ffmpeg=True,
        upload_limit_bytes=CLOUD_LIMIT,
    )
    assert capability.formats == ("mp3", "m4a", "opus")


def test_lossless_stays_when_the_transport_carries_it() -> None:
    capability = audio_capability(
        source_ext="m4a",
        duration_s=SOUNDCLOUD_S,
        has_ffmpeg=True,
        upload_limit_bytes=LOCAL_LIMIT,
    )
    assert capability.formats == ("mp3", "m4a", "flac", "opus", "wav")


def test_an_unknown_length_never_hides_lossless() -> None:
    """Nothing learned contradicts it — an absent fact hides nothing."""
    capability = audio_capability(
        source_ext="m4a",
        duration_s=0,
        has_ffmpeg=True,
        upload_limit_bytes=CLOUD_LIMIT,
    )
    assert "flac" in capability.formats and "wav" in capability.formats


# ---------------------------------------------------------------------------
# The source stream: what an untouched pick would arrive in
# ---------------------------------------------------------------------------


def test_the_source_ext_names_what_the_untouched_pick_arrives_in() -> None:
    """The selector's own first pick: an m4a audio stream when the source has one."""
    info: dict[str, Any] = {
        "formats": [
            {"ext": "webm", "acodec": "opus", "vcodec": "none", "abr": 160},
            {"ext": "m4a", "acodec": "mp4a", "vcodec": "none", "abr": 128},
        ]
    }
    assert source_audio_ext(info) == "m4a"


def test_the_best_audio_stream_stands_when_no_m4a_exists() -> None:
    info: dict[str, Any] = {
        "formats": [
            {"ext": "webm", "acodec": "opus", "vcodec": "none", "abr": 160},
            {"ext": "mp3", "acodec": "mp3", "vcodec": "none", "abr": 96},
        ]
    }
    assert source_audio_ext(info) == "webm"


def test_a_muxed_only_source_names_its_own_container() -> None:
    """A copy of it would arrive as .mp4 — so no button may promise .m4a."""
    info: dict[str, Any] = {
        "ext": "mp4",
        "formats": [{"ext": "mp4", "acodec": "aac", "vcodec": "h264", "tbr": 800}],
    }
    assert source_audio_ext(info) == "mp4"


def test_no_audio_stream_means_no_container_to_promise() -> None:
    info: dict[str, Any] = {
        "formats": [{"ext": "webm", "acodec": "none", "vcodec": "vp9", "tbr": 900}]
    }
    assert source_audio_ext(info) is None


# ---------------------------------------------------------------------------
# The menus, drawn from facts
# ---------------------------------------------------------------------------


def _taps(keyboard: Any) -> list[tuple[str, str]]:
    return [
        (button.text, button.callback_data or "")
        for row in keyboard.inline_keyboard
        for button in row
    ]


def test_a_real_format_list_never_collapses_to_the_download_row() -> None:
    keyboard = user_module._question_keyboard(
        "https://youtu.be/abc",
        EN,
        options=(VideoOption(1080, 21_400_000, True), VideoOption(720, 8_700_000, False)),
    )
    data = [callback for _label, callback in _taps(keyboard)]
    assert "fmt:video:1080" in data and "fmt:video:720" in data
    assert "fmt:video:best" not in data, "a probed ladder is never one «Download» row"


def test_a_post_link_with_real_resolutions_shows_them_too() -> None:
    """A media-kind route used to bury a valid format list before it was drawn."""
    keyboard = user_module._question_keyboard(
        "https://x.com/a/status/1",
        FA,
        options=(VideoOption(2160, 76_200_000, True), VideoOption(1080, 21_400_000, True)),
    )
    data = [callback for _label, callback in _taps(keyboard)]
    assert "fmt:video:2160" in data and "fmt:video:1080" in data


def test_a_single_resolution_source_gets_its_one_real_row() -> None:
    keyboard = user_module._question_keyboard(
        "https://youtu.be/abc", EN, options=(VideoOption(480, 4_200_000, False),)
    )
    data = [callback for _label, callback in _taps(keyboard)]
    assert [item for item in data if item.startswith("fmt:video:")] == ["fmt:video:480"]


def test_a_row_the_source_never_measured_says_the_size_is_unknown() -> None:
    """The resolution is real and stays; the missing number says it is missing."""
    rows = user_module._sized_quality_rows([VideoOption(240, 0, False)], EN)
    assert rows == [(f"240p · {t('media.size_unknown', EN)}", "fmt:video:240")]


def test_the_format_grid_follows_the_capability_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_module.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    capability = audio_capability(
        source_ext="webm",
        duration_s=SOUNDCLOUD_S,
        has_ffmpeg=True,
        upload_limit_bytes=CLOUD_LIMIT,
    )
    keyboard = user_module._question_keyboard(URL, EN, capability=capability)
    labels = [label for label, _callback in _taps(keyboard)]
    assert t("fmt.fmt_mp3", EN) in labels
    assert t("fmt.fmt_m4a", EN) in labels
    assert t("fmt.fmt_opus", EN) in labels
    assert t("fmt.fmt_flac", EN) not in labels, "a 50 MB transport carries no lossless here"
    assert t("fmt.fmt_wav", EN) not in labels


def test_a_crafted_audio_tap_is_answered_from_what_the_question_offered() -> None:
    data: dict[str, Any] = {"audio_offered": ["mp3", "m4a", "opus"], "copy_ok": False}
    assert user_module._tap_was_offered(URL, "audio", "mp3.best", data) is True
    assert user_module._tap_was_offered(URL, "audio", "flac", data) is False
    assert user_module._tap_was_offered(URL, "audio", "m4a", data) is False
    data["copy_ok"] = True
    assert user_module._tap_was_offered(URL, "audio", "m4a", data) is True


# ---------------------------------------------------------------------------
# The probe's budget — the menu is drawn from a list that is really there
# ---------------------------------------------------------------------------


async def test_the_probe_waits_for_the_extractors_own_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clipping the wait shorter than the extraction's own contract cancelled the
    probe and discarded a valid format list — the live «Download»-only menu."""
    seen: dict[str, float] = {}

    async def fake_wait_for(coro: Any, timeout: float | None = None) -> Any:
        seen["timeout"] = float(timeout or 0)
        return await coro

    monkeypatch.setattr(user_module.asyncio, "wait_for", fake_wait_for)

    class _Extractor:
        timeout_s = 41

        async def extract(self, url: str) -> MediaInfo:
            return cast(MediaInfo, SimpleNamespace(title="T"))

    bot = cast(Any, SimpleNamespace(state=SimpleNamespace(extractor=_Extractor())))
    info = await user_module._probe_meta(bot, "https://youtu.be/x")
    assert info is not None
    assert seen["timeout"] >= 41, "the probe must never clip the extractor's own budget"


async def test_a_swallowed_probe_failure_still_speaks_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Extractor:
        timeout_s = 5

        async def extract(self, url: str) -> MediaInfo:
            raise ExtractionError("EXTRACTOR_BLOCKED", "site refused")

    bot = cast(Any, SimpleNamespace(state=SimpleNamespace(extractor=_Extractor())))
    with caplog.at_level(logging.WARNING):
        assert await user_module._probe_meta(bot, "https://youtu.be/x") is None
    assert any(
        "metadata probe gave nothing" in record.getMessage() for record in caplog.records
    ), "a swallowed failure must leave its real name in the log"


# ---------------------------------------------------------------------------
# Canonical URLs — the Reddit /s/ shape of the live case
# ---------------------------------------------------------------------------


def test_the_unwrap_peels_a_viewer_wrapper_to_the_file_inside() -> None:
    wrapped = "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fa.jpeg"
    assert content.unwrap_media_url(wrapped) == "https://i.redd.it/a.jpeg"
    plain = "https://i.redd.it/a.jpeg"
    assert content.unwrap_media_url(plain) == plain
    page = "https://example.com/page?url=https%3A%2F%2Fexample.com%2Fstory"
    assert content.unwrap_media_url(page) == page, "a parameter naming a page is not a wrapper"


class _FakeResponse:
    def __init__(self, url: str) -> None:
        self.url = url

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeSession:
    """Pretends to be Reddit: the share link redirects to the media viewer."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(
            "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fk1xcuq8osaqh1.jpeg"
        )


class _NoNetwork(_FakeSession):
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("a catalogued link must never pay for a redirect round trip")


async def test_a_reddit_share_link_resolves_to_the_file_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact live URL: /s/ShareToken → viewer wrapper → the plain JPEG."""
    monkeypatch.setattr(user_module.aiohttp, "ClientSession", _FakeSession)
    resolved = await user_module._canonical_url(
        "https://www.reddit.com/r/Aitoolsubs/s/d8L1HedJIy"
    )
    assert resolved == "https://i.redd.it/k1xcuq8osaqh1.jpeg"


async def test_a_catalogued_or_plain_link_pays_for_no_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(user_module.aiohttp, "ClientSession", _NoNetwork)
    assert await user_module._canonical_url("https://youtu.be/abc") == "https://youtu.be/abc"
    assert await user_module._canonical_url("https://example.com/x") == "https://example.com/x"


async def test_an_unresolvable_share_link_keeps_itself_and_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken(_FakeSession):
        def get(self, url: str, **kwargs: object) -> _FakeResponse:
            raise OSError("connection refused")

    monkeypatch.setattr(user_module.aiohttp, "ClientSession", _Broken)
    url = "https://www.reddit.com/r/Aitoolsubs/s/d8L1HedJIy"
    assert await user_module._canonical_url(url) == url


# ---------------------------------------------------------------------------
# Delivery failures have their own category — never "a problem on our side"
# ---------------------------------------------------------------------------


async def _finish_with_send_error(
    exc: BaseException, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ExtractionError:
    job = tmp_path / "job"
    job.mkdir()
    media = job / "clip.flac"
    media.write_bytes(b"x" * 2048)
    result = DownloadResult(
        file_path=media,
        info=cast(
            MediaInfo,
            SimpleNamespace(
                title="Helia",
                platform="soundcloud",
                label_p=None,
                height=None,
                audio_kbps=None,
            ),
        ),
        media_format="audio",
        quality="flac",
    )
    task = cast(
        Any,
        SimpleNamespace(
            url=URL,
            telegram_id=1,
            chat_id=1,
            media_format="audio",
            quality="flac",
            lang=EN,
            title="Helia",
        ),
    )

    async def _raise(*args: object, **kwargs: object) -> Any:
        raise exc

    monkeypatch.setattr(worker_module, "_upload", _raise)
    with pytest.raises(ExtractionError) as raised:
        await worker_module._finish_upload(
            task,
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            result,
        )
    return raised.value


async def test_a_too_large_send_is_a_file_size_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live SoundCloud failure: 57 MB into a 50 MB wall said «internal error»."""
    error = await _finish_with_send_error(
        TelegramEntityTooLarge(cast(Any, SimpleNamespace()), "request entity too large"),
        tmp_path,
        monkeypatch,
    )
    assert error.code == "FILE_TOO_LARGE"


async def test_a_refused_send_is_a_delivery_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = await _finish_with_send_error(
        TelegramBadRequest(cast(Any, SimpleNamespace()), "bad request"), tmp_path, monkeypatch
    )
    assert error.code == "DELIVERY_FAILED"
