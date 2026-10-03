"""C1: no log line carries a raw URL anymore.

The five remaining ``%.80s`` log sites in handlers/user.py now log
``telemetry.log_url`` (host + digest). These tests feed URLs carrying a
query token, signed CDN params, userinfo and a fragment through each site
and assert the secrets never reach the log while host and digest do.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from test_user_menu import (
    RecordingBot,
    _fake_queue,
    _fresh_state,
    _let_the_delete_land,
    _message,
    _RecordingProbe,
    _user,
    no_cached_rows,
)

from handlers import user as user_module
from services.telemetry import log_url

SECRET_URL = "https://user:pw_X1Q2@example.com:8443/v/abc?token=tok_X2W3&sig=sig_X3E4#frag_X4R5"
SECRETS = ("pw_X1Q2", "tok_X2W3", "sig_X3E4", "frag_X4R5")


def _assert_redacted(text: str, *urls: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"secret {secret!r} leaked into logs"
    for url in urls:
        assert url not in text, "raw URL leaked into logs"
        assert log_url(url) in text, "host+digest must stay for debugging"


async def _supported(url: str) -> bool:
    return True


async def _same(url: str) -> str:
    return url


async def test_intake_completion_log_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(user_module, "_probe_supported", _supported)
    monkeypatch.setattr(user_module, "_canonical_url", _same)
    monkeypatch.setattr(user_module.cache_service, "get_cached_rows", no_cached_rows)
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=_RecordingProbe())

    with caplog.at_level("INFO", logger="handlers.user"):
        await user_module.on_text_with_url(
            _message(SECRET_URL, bot),
            _fresh_state(),
            _user(),
            object(),
            _fake_queue(),
            bot,
            lang="en",
        )
    await _let_the_delete_land()

    completions = [
        record.getMessage() for record in caplog.records if "intake flow for" in record.getMessage()
    ]
    assert completions, "the intake still logs its timing line"
    for line in completions:
        _assert_redacted(line, SECRET_URL)


async def test_spotify_miss_log_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _boom(url: str) -> Any:
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(user_module, "_probe_supported", _supported)
    monkeypatch.setattr(user_module, "_canonical_url", _same)
    monkeypatch.setattr(user_module.cache_service, "get_cached_rows", no_cached_rows)
    monkeypatch.setattr(user_module.spotify, "lookup", _boom)
    bot = RecordingBot()
    bot.state = SimpleNamespace(extractor=_RecordingProbe())
    url = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=tok_X2W3"

    with caplog.at_level("WARNING", logger="handlers.user"):
        await user_module.on_text_with_url(
            _message(url, bot),
            _fresh_state(),
            _user(),
            object(),
            _fake_queue(),
            bot,
            lang="en",
        )
    await _let_the_delete_land()

    misses = [
        record.getMessage()
        for record in caplog.records
        if "spotify lookup gave nothing" in record.getMessage()
    ]
    assert misses, "a failed spotify lookup still logs one warning"
    for line in misses:
        assert "tok_X2W3" not in line
        assert url not in line
        assert log_url(url) in line


async def test_cache_miss_log_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _down(pool: Any, url: str) -> list[Any]:
        raise ConnectionError("cache is down")

    monkeypatch.setattr(user_module.cache_service, "get_cached_rows", _down)

    with caplog.at_level("DEBUG", logger="handlers.user"):
        assert await user_module._cached_rows(object(), SECRET_URL) == []

    misses = [
        record.getMessage() for record in caplog.records if "cache lookup gave nothing" in record.getMessage()
    ]
    assert misses, "a failed cache lookup still logs at debug"
    for line in misses:
        _assert_redacted(line, SECRET_URL)


class _Hop:
    """The ``(request, resolved)`` pair ``guarded_get`` yields."""

    def __init__(self, resolved: str) -> None:
        self._resolved = resolved

    async def __aenter__(self) -> tuple[Any, str]:
        return (None, self._resolved)

    async def __aexit__(self, *args: Any) -> bool:
        return False


SHARE_URL = "https://user:pw_X1Q2@www.reddit.com/r/x/comments/1/y/?token=tok_X2W3#frag_X4R5"


async def test_share_link_unresolved_log_is_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _unsupported(url: str) -> bool:
        return False

    def _boom(*args: Any, **kwargs: Any) -> _Hop:
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(user_module.ExtractorService, "is_url_supported", _unsupported)
    monkeypatch.setattr(user_module.host_guard, "guarded_get", _boom)

    with caplog.at_level("INFO", logger="handlers.user"):
        assert await user_module._canonical_url(SHARE_URL) == SHARE_URL

    lines = [record.getMessage() for record in caplog.records if "did not resolve" in record.getMessage()]
    assert lines, "an unresolvable share link still logs one line"
    for line in lines:
        _assert_redacted(line, SHARE_URL)


async def test_share_link_resolved_log_redacts_both_urls(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    resolved = "https://www.reddit.com/r/x/comments/2/z/?sig=sig_X3E4&exp=999"

    def _only_resolved_supported(url: str) -> bool:
        return url == resolved

    def _hop(*args: Any, **kwargs: Any) -> _Hop:
        return _Hop(resolved)

    monkeypatch.setattr(user_module.ExtractorService, "is_url_supported", _only_resolved_supported)
    monkeypatch.setattr(user_module.host_guard, "guarded_get", _hop)

    with caplog.at_level("INFO", logger="handlers.user"):
        assert await user_module._canonical_url(SHARE_URL) == resolved

    lines = [record.getMessage() for record in caplog.records if "resolves to" in record.getMessage()]
    assert lines, "an adopted redirect still logs the hop"
    for line in lines:
        _assert_redacted(line, SHARE_URL, resolved)
