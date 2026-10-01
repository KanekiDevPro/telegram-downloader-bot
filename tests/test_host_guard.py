"""The SSRF host guard (P1-2): one policy, fakes only, no real network.

Every case pins the central helper in :mod:`services.host_guard` — literal IP
forms, local/internal names, the compose service list, userinfo tricks,
platform DNS-skipping, DNS verdicts, redirect re-validation and digest-only
logging. Resolvers and HTTP sessions are injected doubles; a resolver that
explodes proves the paths that must never touch DNS.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Sequence

import pytest

from services import host_guard
from services.host_guard import HostGuardError, HostResolver

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.9.8.7"


async def _no_dns(host: str) -> Sequence[str]:
    raise AssertionError(f"DNS must not be consulted for {host!r}")


def _resolver(
    mapping: dict[str, Sequence[str] | Exception], calls: list[str] | None = None
) -> HostResolver:
    async def resolve(host: str) -> Sequence[str]:
        if calls is not None:
            calls.append(host)
        answer = mapping.get(host, OSError(f"no such host: {host}"))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return resolve


async def _verdict(
    url: str, resolver: HostResolver | None = _no_dns, **kwargs: Any
) -> host_guard.HostVerdict:
    return await host_guard.check_url(url, resolve=resolver, **kwargs)


# ---------------------------------------------------------------------------
# Literal IPs: every form resolvers accept must be judged, never DNS-looked-up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://localhost./",
        "http://LOCALHOST/",
        "http://127.0.0.1/",
        "http://127.0.0.1./",
        "http://127.0.0.1:8080/x",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://0x7F.0x0.0x0.0x1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::1%lo]/",
        "http://10.0.0.1/",
        "http://192.168.1.5:9000/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://169.254.169.254/latest/meta-data/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://255.255.255.255/",
        "http://100.64.0.1/",
        "http://[fe80::1]/",
        "http://[fc00::1]/",
        "http://[ff02::1]/",
    ],
)
async def test_private_literals_are_refused_without_dns(url: str) -> None:
    verdict = await _verdict(url)

    assert verdict.ok is False
    assert verdict.reason in {"private-ip", "local-name"}


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8/",
        "http://1.1.1.1:8080/x",
        "http://172.32.0.1/",
        "http://[2606:4700:4700::1111]/",
        "http://[::ffff:8.8.8.8]/",
    ],
)
async def test_global_literals_pass_without_dns(url: str) -> None:
    assert (await _verdict(url)).ok is True


def test_octal_forms_follow_inet_aton_not_float_gaps() -> None:
    # 0177.0.0.1 is 127.0.0.1 in the octal spelling resolvers accept.
    assert host_guard._loose_ipv4_int("0177.0.0.1") == (127 << 24 | 1)
    assert host_guard._loose_ipv4_int("2130706433") == (127 << 24 | 1)
    assert host_guard._loose_ipv4_int("0x7f000001") == (127 << 24 | 1)
    assert host_guard._loose_ipv4_int("127.1") == (127 << 24 | 1)
    assert host_guard._loose_ipv4_int("999.1.1.1") is None
    assert host_guard._loose_ipv4_int("1.2.3.4.5") is None
    assert host_guard._loose_ipv4_int("example.com") is None


# ---------------------------------------------------------------------------
# Names: local families, userinfo tricks, trailing dots, IDN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://mybox.localhost/",
        "http://printer.local/",
        "http://db.internal/",
        "http://api.staging.internal:8080/x",
    ],
)
async def test_local_name_families_are_refused_without_dns(url: str) -> None:
    verdict = await _verdict(url)

    assert verdict.ok is False
    assert verdict.reason == "local-name"


@pytest.mark.parametrize(
    "url",
    [
        "http://good.com@127.0.0.1/",
        "http://user:pass@127.0.0.1/",
        "http://127.0.0.1@good.com/",
        "http://user:pw@cobalt:9000/",
    ],
)
async def test_userinfo_is_refused_whichever_side_it_decorates(url: str) -> None:
    assert (await _verdict(url)).ok is False


async def test_trailing_dots_do_not_smuggle_names_past_the_lists() -> None:
    assert (await _verdict("http://localhost./")).ok is False
    calls: list[str] = []
    verdict = await host_guard.check_url(
        "http://public.example./file.mp4",
        resolve=_resolver({"public.example": [PUBLIC_IP]}, calls),
    )

    assert verdict.ok is True
    assert calls == ["public.example"]


async def test_idn_is_compared_and_resolved_as_punycode() -> None:
    calls: list[str] = []
    verdict = await host_guard.check_url(
        "http://münchen.de/",
        resolve=_resolver({"xn--mnchen-3ya.de": [PUBLIC_IP]}, calls),
    )

    assert verdict.ok is True
    assert calls == ["xn--mnchen-3ya.de"]


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "not-a-url", ""])
async def test_non_http_schemes_and_bare_words_are_refused(url: str) -> None:
    assert (await _verdict(url)).ok is False


# ---------------------------------------------------------------------------
# Compose service names: read from the real file, never from memory
# ---------------------------------------------------------------------------


def _compose_names_from_the_real_file() -> set[str]:
    """Independent read of docker-compose.yml: top-level keys of `services:`."""
    text = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    names: set[str] = set()
    in_services = False
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            if not in_services:
                continue
            match = re.match(r"^  ([A-Za-z0-9][A-Za-z0-9_-]*)\s*:", line.split("#", 1)[0])
            if match:
                names.add(match.group(1).lower())
            continue
        in_services = line.split("#", 1)[0].strip() == "services:"
    return names


def test_the_guard_reads_the_real_compose_file() -> None:
    names = _compose_names_from_the_real_file()

    assert names, "docker-compose.yml must name services for the guard to block"
    assert host_guard.COMPOSE_HOSTNAMES == names


async def test_every_compose_service_name_is_refused() -> None:
    names = _compose_names_from_the_real_file()
    assert names

    for name in sorted(names):
        for url in (f"http://{name}/", f"http://{name}:9000/tunnel?id=x"):
            verdict = await _verdict(url)
            assert verdict.ok is False, url
            assert verdict.reason == "internal-name", url


async def test_an_operator_allowlist_covers_only_its_own_hosts() -> None:
    name = sorted(_compose_names_from_the_real_file())[0]

    assert (await _verdict(f"http://{name}:9000/t/x")).ok is False
    assert (await _verdict(f"http://{name}:9000/t/x", allow_hosts={name})).ok is True
    # The allowlist is exact: a lookalike still takes the DNS path (and only it).
    calls: list[str] = []
    verdict = await host_guard.check_url(
        f"http://{name}.evil.test/",
        allow_hosts={name},
        resolve=_resolver({f"{name}.evil.test": [PUBLIC_IP]}, calls),
    )
    assert calls == [f"{name}.evil.test"]
    assert verdict.ok is True


# ---------------------------------------------------------------------------
# Platform hosts skip DNS (exact or dot-suffix, never substring)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://old.reddit.com/media?url=https://i.redd.it/x.jpeg",
        "https://vm.tiktok.com/abc/",
        "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
        "https://spotify.link/abcDEF123",
    ],
)
async def test_platform_hosts_skip_dns(url: str) -> None:
    assert (await host_guard.check_url(url, resolve=_no_dns)).ok is True


async def test_substring_spoof_hosts_are_not_platforms() -> None:
    calls: list[str] = []
    verdict = await host_guard.check_url(
        "https://youtube.com.evil.test/watch?v=x",
        resolve=_resolver({"youtube.com.evil.test": [PUBLIC_IP]}, calls),
    )

    # Only DNS could allow it — the platform skip must not have fired.
    assert calls == ["youtube.com.evil.test"]
    assert verdict.ok is True


# ---------------------------------------------------------------------------
# DNS verdicts: ANY non-global address refuses; failure policy is explicit
# ---------------------------------------------------------------------------


async def test_a_hostname_resolving_private_is_refused() -> None:
    verdict = await host_guard.check_url(
        "https://files.evil.test/v.mp4",
        resolve=_resolver({"files.evil.test": [PRIVATE_IP]}),
    )

    assert verdict.ok is False
    assert verdict.reason == "dns-private"


async def test_one_private_address_among_public_ones_still_refuses() -> None:
    verdict = await host_guard.check_url(
        "https://cdn.evil.test/v.mp4",
        resolve=_resolver({"cdn.evil.test": [PUBLIC_IP, PRIVATE_IP]}),
    )

    assert verdict.ok is False
    assert verdict.reason == "dns-private"


async def test_a_public_hostname_passes_with_its_query_preserved() -> None:
    calls: list[str] = []
    url = "https://cdn.example.com/file.mp4?token=SECRET&sig=abc"
    verdict = await host_guard.check_url(
        url, resolve=_resolver({"cdn.example.com": [PUBLIC_IP]}, calls)
    )

    assert verdict.ok is True
    assert calls == ["cdn.example.com"]
    assert verdict.host == "cdn.example.com"
    assert "SECRET" not in verdict.host + verdict.reason


@pytest.mark.parametrize("failure", [OSError("down"), TimeoutError(), []])
async def test_dns_failure_is_fail_closed_by_default(failure: Any) -> None:
    async def resolve(host: str) -> Sequence[str]:
        if isinstance(failure, list):
            return failure
        raise failure

    verdict = await host_guard.check_url("https://files.example.com/v.mp4", resolve=resolve)

    assert verdict.ok is False
    assert verdict.reason == "dns-failed"


async def test_dns_failure_defers_only_for_the_intake_triage() -> None:
    async def resolve(host: str) -> Sequence[str]:
        raise OSError("offline")

    verdict = await host_guard.check_url(
        "https://files.example.com/v.mp4",
        resolve=resolve,
        fail_open_on_dns_failure=True,
    )

    assert verdict.ok is True


async def test_a_hanging_resolver_is_bounded_by_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_guard, "DNS_TIMEOUT_S", 0.05)

    async def slow(host: str) -> Sequence[str]:
        await __import__("asyncio").sleep(5)
        return [PUBLIC_IP]

    verdict = await host_guard.check_url("https://slow.example.com/", resolve=slow)

    assert verdict.ok is False
    assert verdict.reason == "dns-failed"


# ---------------------------------------------------------------------------
# Redirects: every hop re-validated, never auto-followed
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"data",
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _StubResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _StubSession:
    def __init__(self, routes: dict[str, _StubResponse]) -> None:
        self._routes = routes
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _StubResponse:
        self.requests.append({"url": url, **kwargs})
        assert url in self._routes, f"unexpected fetch of {url!r}"
        return self._routes[url]


def _public_resolver(calls: list[str] | None = None) -> HostResolver:
    return _resolver({"cdn.example.com": [PUBLIC_IP]}, calls)


async def test_a_redirect_hop_to_a_private_address_is_rejected() -> None:
    start = "https://cdn.example.com/file.mp4?token=SECRET&sig=abc"
    session = _StubSession(
        {start: _StubResponse(status=302, headers={"Location": "http://127.0.0.1/admin"})}
    )

    with pytest.raises(HostGuardError):
        async with host_guard.guarded_get(
            session, start, resolve=_public_resolver()
        ) as (_response, _final):
            pass  # pragma: no cover — the hop must raise first

    assert [request["url"] for request in session.requests] == [start]
    # The signed query rode along untouched, and was never fetched elsewhere.
    assert session.requests[0]["url"] == start


async def test_public_redirect_chains_resolve_including_relative_hops() -> None:
    second = "https://cdn.example.com/other.bin"
    session = _StubSession(
        {
            "https://cdn.example.com/file.mp4": _StubResponse(
                status=307, headers={"Location": "/other.bin"}
            ),
            second: _StubResponse(status=200, body=b"bytes"),
        }
    )

    async with host_guard.guarded_get(
        session, "https://cdn.example.com/file.mp4", resolve=_public_resolver()
    ) as (response, final):
        body = await response.read()

    assert final == second
    assert body == b"bytes"
    assert [request["url"] for request in session.requests] == [
        "https://cdn.example.com/file.mp4",
        second,
    ]
    # Every request went out with auto-follow off — the guard owns the hops.
    assert all(request["allow_redirects"] is False for request in session.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_all_redirect_statuses_are_followed_manually(status: int) -> None:
    end = "https://cdn.example.com/end.bin"
    session = _StubSession(
        {
            "https://cdn.example.com/start": _StubResponse(
                status=status, headers={"Location": end}
            ),
            end: _StubResponse(status=200),
        }
    )

    async with host_guard.guarded_get(
        session, "https://cdn.example.com/start", resolve=_public_resolver()
    ) as (_response, final):
        assert final == end


async def test_a_non_http_redirect_target_is_refused() -> None:
    session = _StubSession(
        {
            "https://cdn.example.com/x": _StubResponse(
                status=302, headers={"Location": "ftp://cdn.example.com/x"}
            )
        }
    )

    with pytest.raises(HostGuardError) as exc_info:
        async with host_guard.guarded_get(
            session, "https://cdn.example.com/x", resolve=_public_resolver()
        ):
            pass  # pragma: no cover

    assert exc_info.value.verdict.reason == "bad-redirect"


async def test_redirect_loops_give_up_after_the_budget() -> None:
    routes = {
        f"https://cdn.example.com/hop{i}": _StubResponse(
            status=302, headers={"Location": f"https://cdn.example.com/hop{i + 1}"}
        )
        for i in range(8)
    }
    session = _StubSession(routes)

    with pytest.raises(HostGuardError) as exc_info:
        async with host_guard.guarded_get(
            session,
            "https://cdn.example.com/hop0",
            resolve=_resolver({"cdn.example.com": [PUBLIC_IP]}),
        ):
            pass  # pragma: no cover

    assert exc_info.value.verdict.reason == "too-many-redirects"


# ---------------------------------------------------------------------------
# Diaries, not evidence: digests in logs, catalogue words for users
# ---------------------------------------------------------------------------


def test_host_digests_are_stable_short_and_content_free() -> None:
    first = host_guard.HostVerdict(False, "private-ip", "127.0.0.1").host_digest
    again = host_guard.HostVerdict(False, "dns-private", "127.0.0.1").host_digest
    other = host_guard.HostVerdict(False, "private-ip", "10.0.0.1").host_digest

    assert first == again == hashlib.sha256(b"127.0.0.1").hexdigest()[:16]
    assert len(first) == 16
    assert other != first
    assert "127.0.0.1" not in first


def test_refusal_errors_carry_no_url_or_address() -> None:
    error = HostGuardError(host_guard.HostVerdict(False, "dns-private", "secret.internal"))

    assert "secret.internal" not in str(error)
    assert "169.254" not in str(error)


def test_refusal_texts_exist_in_both_languages() -> None:
    from core.catalog import MESSAGES

    for key in ("err.PRIVATE_HOST", "intake.private_host"):
        assert MESSAGES[key]["en"].strip(), key
        assert MESSAGES[key]["fa"].strip(), key
