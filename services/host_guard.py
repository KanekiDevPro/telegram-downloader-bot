"""One policy for every server-side fetch of a stranger's URL (SSRF guard).

User-controlled links reach several fetch paths (intake canonical-resolve,
Spotify page/cover fetch, Cobalt file download, yt-dlp, the Cobalt API). This
module is the single place that decides whether a URL may be fetched from
this host — every boundary imports it, none re-implements it.

What it enforces, in order:

1. ``http``/``https`` only, no credentials in the URL (``user@host`` tricks
   and credential leakage share one refusal).
2. Literal IPs — standard, IPv4-mapped IPv6 and zone ids via :mod:`ipaddress`,
   plus the loose decimal/hex/octal/short IPv4 forms resolvers accept
   (``2130706433``, ``0x7f000001``, ``0177.0.0.1``, ``127.1``) — must be
   globally reachable and not multicast (``is_global``/``is_multicast``,
   never a hand-written range list).
3. Local names (``localhost``, trailing-dot, ``*.localhost``, ``*.local``,
   ``*.internal``) and this stack's own compose service names are refused.
4. Known platform hosts (exact host or proper dot-suffix, never substring)
   skip DNS: platform availability must not depend on the resolver.
5. Anything else is resolved (non-blocking, with a timeout) and refused when
   ANY address is non-global — cloud metadata (``169.254.169.254`` included.

DNS-failure policy: fail-closed by default (direct-file/cover fetches). The
intake triage instead defers (``fail_open_on_dns_failure``): triage must not
couple answering to DNS — whatever it cannot prove is re-checked at the fetch
sites, which all fail closed.

What this CANNOT cover (documented, not fixed): yt-dlp and the Cobalt API
resolve DNS and follow redirects internally — a pre-check cannot stop their
redirect-to-internal or rebinding. See ``docs/README.md`` ("Host guard") for
the residual risk and the recommended network-level controls.

Logs and user messages never carry the URL or the resolved address — only
:attr:`HostVerdict.host_digest`. Refusals surface through the catalogue
(``err.PRIVATE_HOST`` for engine paths, ``intake.private_host`` at intake).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from services.content import PLATFORM_HOSTS

logger = logging.getLogger(__name__)

#: How long a DNS lookup may stall a fetch decision. The resolver runs off
#: the loop; this only bounds the wait, never the loop itself.
DNS_TIMEOUT_S = 3.0

#: Redirect hops followed per fetch before giving up (same order as the
#: default most clients used before manual following took over).
MAX_REDIRECTS = 5

#: Statuses that carry a ``Location`` hop (300/304/305 are not followed: 300
#: needs a choice, 304 has no new location, 305 is deprecated and unsafe).
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: A resolver maps a hostname to its address strings. The default consults
#: the system resolver off the loop; tests inject fakes (never real DNS).
HostResolver = Callable[[str], Awaitable[Sequence[str]]]


def _read_compose_service_names() -> frozenset[str]:
    """The service names of this stack, read from the real compose file.

    Parsed, not remembered: the names live in ``docker-compose.yml`` next to
    this package, and this reads them (top-level keys of the ``services:``
    block). A missing/unreadable file leaves the set empty — the literal-IP
    and DNS rules below still apply; only the fast name match is lost.
    """
    path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("host guard: docker-compose.yml unreadable — service-name blocklist empty")
        return frozenset()
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
    return frozenset(names)


#: Internal hostnames of this stack: its compose services (``cobalt``,
#: ``redis``, … — whatever the file actually names) plus the gateway name
#: compose gives the host (``extra_hosts`` in the same file). A user link
#: naming any of these is refused; operator configuration may explicitly
#: allow its own instance hosts per call (``allow_hosts``), never user input.
COMPOSE_HOSTNAMES: frozenset[str] = _read_compose_service_names()

#: The one gateway alias this file knows: it appears in docker-compose.yml's
#: ``extra_hosts`` and reaches the host itself, so it is internal by nature.
INTERNAL_NAMES: frozenset[str] = COMPOSE_HOSTNAMES | {"host.docker.internal"}

#: Hosts whose URLs skip DNS resolution: the advertised platform table, plus
#: Spotify's front doors (``open.spotify.com`` embeds and ``spotify.link``
#: shares — the section needles in ``services.content``, mirrored here so the
#: guard reads one set). Exact host or proper dot-suffix only, like
#: :func:`services.content.claims_platform` — never substring.
PLATFORM_SKIP_HOSTS: frozenset[str] = PLATFORM_HOSTS | {"spotify.com", "spotify.link"}


@dataclass(frozen=True)
class HostVerdict:
    """Whether one URL may be fetched from this host, and why not.

    ``host`` is the normalized host (never the raw URL, never an address);
    :meth:`host_digest` is what logs carry — a short hash, not the name.
    """

    ok: bool
    reason: str
    host: str = ""

    @property
    def host_digest(self) -> str:
        """The log-safe fingerprint of the host (16 hex chars of SHA-256)."""
        return hashlib.sha256(self.host.encode("utf-8")).hexdigest()[:16]


class HostGuardError(Exception):
    """A fetch refused by the host policy. Carries the verdict, never the URL."""

    def __init__(self, verdict: HostVerdict) -> None:
        super().__init__(f"refused host ({verdict.reason})")
        self.verdict = verdict


def _loose_ipv4_int(host: str) -> int | None:
    """The 32-bit value of a loose IPv4 literal (``inet_aton`` semantics).

    ``None`` when the host is not such a literal: dotted decimal/octal/hex
    parts (``0177.0.0.1``), short forms (``127.1``), or a bare decimal/hex
    integer (``2130706433``, ``0x7f000001``). Out-of-range parts are not
    literals (``999.1.1.1``, ``1.2.3.4.5``) — those take the DNS path.
    """
    if not host:
        return None
    parts = host.split(".")
    if len(parts) > 4:
        return None
    nums: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                nums.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0") and part.isdigit():
                nums.append(int(part, 8))
            elif part.isdigit():
                nums.append(int(part, 10))
            else:
                return None
        except ValueError:
            return None
    if len(nums) == 4:
        if any(value > 255 for value in nums):
            return None
        return (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
    if len(nums) == 3:
        if nums[0] > 255 or nums[1] > 255 or nums[2] > 0xFFFF:
            return None
        return (nums[0] << 24) | (nums[1] << 16) | nums[2]
    if len(nums) == 2:
        if nums[0] > 255 or nums[1] > 0xFFFFFF:
            return None
        return (nums[0] << 24) | nums[1]
    return nums[0] if nums[0] <= 0xFFFFFFFF else None


def _literal_is_global(host: str) -> bool | None:
    """Globality of a literal IP host, or ``None`` when not a literal.

    A colon forces the issue: colons never survive hostname parsing except in
    IPv6, so an unparseable colon host is refused rather than DNS-looked-up.
    IPv4-mapped IPv6 (``::ffff:127.0.0.1``) is judged by the address it maps.
    """
    address: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if ":" in host:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            number = _loose_ipv4_int(host)
            if number is None:
                return None
            address = ipaddress.IPv4Address(number)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # Multicast reads as global to ``is_global`` but is never a fetch target.
    return address.is_global and not address.is_multicast


def _is_platform_host(host: str) -> bool:
    """Exact host or proper dot-suffix of a skip host — never substring."""
    return any(host == name or host.endswith(f".{name}") for name in PLATFORM_SKIP_HOSTS)


async def _default_resolve(host: str) -> list[str]:
    """The system resolver, off the loop and bounded by :data:`DNS_TIMEOUT_S`."""
    def _lookup() -> list[str]:
        found: list[str] = []
        for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        ):
            ip = sockaddr[0]
            if isinstance(ip, str) and ip not in found:
                found.append(ip)
        return found

    return await asyncio.wait_for(asyncio.to_thread(_lookup), DNS_TIMEOUT_S)


async def _resolve_addresses(host: str, resolve: HostResolver | None) -> list[str]:
    if resolve is not None:
        return list(await asyncio.wait_for(resolve(host), DNS_TIMEOUT_S))
    return await _default_resolve(host)


async def check_url(
    url: str,
    *,
    allow_hosts: Iterable[str] = (),
    resolve: HostResolver | None = None,
    fail_open_on_dns_failure: bool = False,
) -> HostVerdict:
    """Whether ``url`` may be fetched from this host.

    ``allow_hosts`` names operator-configured hosts that bypass the policy
    (the Cobalt instance's own tunnel hosts) — exact match only, never user
    input. ``resolve`` injects DNS (tests use fakes). ``fail_open_on_dns_failure``
    is the intake triage's escape hatch (see the module docstring): fetch
    sites always fail closed.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return HostVerdict(False, "bad-url")
    if parts.scheme.lower() not in ("http", "https"):
        return HostVerdict(False, "bad-scheme")
    if "@" in parts.netloc:
        # ``http://good.com@127.0.0.1/`` fetches 127.0.0.1 with the other's name,
        # and any credentials would ride to wherever they point: refused whole.
        return HostVerdict(False, "userinfo")
    try:
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return HostVerdict(False, "bad-host")
    if not host:
        return HostVerdict(False, "bad-host")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return HostVerdict(False, "bad-host")
    if ascii_host in {name.lower().rstrip(".") for name in allow_hosts}:
        return HostVerdict(True, "ok", ascii_host)
    literal = _literal_is_global(ascii_host)
    if literal is not None:
        return (
            HostVerdict(True, "ok", ascii_host)
            if literal
            else HostVerdict(False, "private-ip", ascii_host)
        )
    if ascii_host == "localhost" or ascii_host.endswith((".localhost", ".local", ".internal")):
        return HostVerdict(False, "local-name", ascii_host)
    if ascii_host in INTERNAL_NAMES:
        return HostVerdict(False, "internal-name", ascii_host)
    if _is_platform_host(ascii_host):
        return HostVerdict(True, "ok", ascii_host)
    try:
        addresses = await _resolve_addresses(ascii_host, resolve)
    except (OSError, asyncio.TimeoutError):
        if fail_open_on_dns_failure:
            # Triage could not prove anything (offline, slow DNS): defer to the
            # fetch sites, which fail closed. Logged by the caller via digest.
            return HostVerdict(True, "ok", ascii_host)
        return HostVerdict(False, "dns-failed", ascii_host)
    if not addresses:
        if fail_open_on_dns_failure:
            return HostVerdict(True, "ok", ascii_host)
        return HostVerdict(False, "dns-failed", ascii_host)
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            return HostVerdict(False, "dns-failed", ascii_host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            return HostVerdict(False, "dns-private", ascii_host)
    return HostVerdict(True, "ok", ascii_host)


@asynccontextmanager
async def guarded_get(
    session: Any,
    url: str,
    *,
    allow_hosts: Iterable[str] = (),
    resolve: HostResolver | None = None,
    max_redirects: int = MAX_REDIRECTS,
    timeout: Any = None,
    headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> AsyncIterator[tuple[Any, str]]:
    """GET ``url`` with the host policy re-checked on the start and every hop.

    Auto-follow stays off: each ``Location`` (absolute or relative) is joined
    onto the current URL and validated before the next request. Yields the
    final, still-open response and its URL; intermediate responses are closed
    as they are left. Non-http(s) locations and hop budgets raise
    :class:`HostGuardError`, as does any hop the policy refuses.
    """
    current = url
    for _ in range(max_redirects + 1):
        verdict = await check_url(current, allow_hosts=allow_hosts, resolve=resolve)
        if not verdict.ok:
            raise HostGuardError(verdict)
        async with session.get(
            current,
            allow_redirects=False,
            headers=headers,
            timeout=timeout,
            proxy=proxy,
        ) as response:
            location = response.headers.get("Location") if response.headers else None
            if response.status in REDIRECT_STATUSES and location:
                nxt = urljoin(current, str(location).strip())
                if urlsplit(nxt).scheme.lower() not in ("http", "https"):
                    raise HostGuardError(HostVerdict(False, "bad-redirect", verdict.host))
                current = nxt
                continue
            yield response, current
            return
    raise HostGuardError(HostVerdict(False, "too-many-redirects", ""))
