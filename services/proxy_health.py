"""The yt-dlp tunnel, asked whether it is one.

``YTDLP_PROXY`` is the only setting in this deployment that can make *every* download
fail at once: yt-dlp sends all of its traffic through it, so a proxy that is down
turns a working bot into one that answers every link with a connection error. Two
facts decide what to do about that, and neither is visible in the configuration:

* **does it answer at all?** — no means downloads must run direct, and an admin has
  to hear about it;
* **where do downloads leave from?** — Cloudflare's WARP reports it on its own trace
  endpoint (``warp=on|plus|off`` plus the ``ip=`` it saw). A proxy that answers with
  ``warp=off`` is not broken; it is still the datacenter address YouTube flagged,
  which is a different problem with a different fix.

Probing *through* the proxy (rather than pinging its port) is what makes the second
answer possible, and the trace endpoint is Cloudflare's own — a WARP tunnel answers
it with no setup at all. When the proxy can only be dialled by a protocol aiohttp
does not speak (``socks5://`` without the extra dependency), the probe degrades to a
TCP connect and says so: it then knows the tunnel is listening, and nothing more.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

#: Cloudflare's trace endpoint — the one WARP's own documentation points at. Tiny,
#: unauthenticated, and it answers with ``warp=`` and the IP it saw.
TRACE_URL = "https://cloudflare.com/cdn-cgi/trace"

#: One probe is a single small request through a tunnel that may still be starting.
PROBE_TIMEOUT_S = 8.0

#: Schemes aiohttp can dial itself. Anything else (``socks5://``, or a bare
#: ``host:port``) falls back to a TCP connect.
_DIRECT_SCHEMES = frozenset({"http", "https"})

#: The port a proxy URL without one is assumed to use (gost's default in the WARP image).
DEFAULT_PROXY_PORT = 1080


@dataclass(frozen=True)
class TunnelHealth:
    """What the proxy is, after being asked once."""

    url: str = ""
    reachable: bool = False
    #: Whether the trace was actually read. A SOCKS5 proxy cannot be dialled by
    #: aiohttp without another dependency, so it degrades to "something is
    #: listening" — and a deployment that *cannot* be asked where it goes must not be
    #: reported as one that goes the wrong way. This is the difference between
    #: "warp=off" and "unknown".
    traced: bool = False
    #: ``on`` / ``plus`` / ``off`` from the trace, or ``""`` when it could not be read.
    warp: str = ""
    #: The address the *outside* saw — the one fact that explains a YouTube block.
    exit_ip: str = ""
    detail: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def on_warp(self) -> bool:
        """True when the trace says this traffic really left through WARP."""
        return self.warp in {"on", "plus"}

    @property
    def wrong_exit(self) -> bool:
        """Whether this is a tunnel that *works* and still leaves from the host.

        Deliberately not the complement of "healthy": it needs the trace (a SOCKS5
        proxy answers without ever saying where it goes, so nothing can be concluded
        about its exit) and it is the one failure a ❌ would mis-describe — the tunnel
        is up, and the address YouTube sees is unchanged.
        """
        return self.reachable and self.traced and not self.on_warp

    @property
    def usable(self) -> bool:
        """Whether yt-dlp should be sent through it. Only a *silent* proxy is not.

        ``warp=off`` stays usable on purpose: it is no worse than going direct, and
        dropping it would silently discard a setting the operator wrote.
        """
        return self.reachable

    @property
    def state(self) -> str:
        """One word for the watch: ``off`` | ``ok`` | ``drift`` | ``down``."""
        if not self.configured:
            return "off"
        if not self.reachable:
            return "down"
        if not self.traced:
            # Answers, and where it goes cannot be read (a SOCKS5 proxy: aiohttp does
            # not speak it). Calling that ``drift`` would page the admins every hour
            # for the deployment that did exactly what it was told.
            return "ok"
        return "ok" if self.on_warp else "drift"

    def describe(self) -> str:
        """What it is, in one log line."""
        if not self.configured:
            return "تنظیم نشده (YTDLP_PROXY خالی است)"
        if not self.reachable:
            return f"{self.url} — پاسخ نداد ({self.detail})"
        if not self.traced:
            return f"{self.url} — {self.detail}"
        where = f"warp={self.warp}" if self.warp else "warp=?"
        if self.exit_ip:
            where += f" از {self.exit_ip}"
        return f"{self.url} — {where}"

    def line(self) -> str:
        """The `/doctor` row: what was asked, what answered, and what it means."""
        if not self.configured:
            return "🔀 تونل: ⚫️ خاموش — دانلودها مستقیم از IP همین هاست می‌روند"
        if not self.reachable:
            return (
                f"🔀 تونل: ❌ {self.url} پاسخ نمی‌دهد — دانلودها همین حالا مستقیم "
                f"می‌روند ({self.detail})"
            )
        if self.on_warp:
            where = f"، خروج {self.exit_ip}" if self.exit_ip else ""
            return f"🔀 تونل: 🟢 {self.url} — warp={self.warp}{where}"
        if not self.traced:
            return (
                f"🔀 تونل: 🟢 {self.url} — {self.detail}; مسیر خروج قابل خواندن نیست "
                "(پروتکل SOCKS5) پس warp تأیید نشده"
            )
        return (
            f"🔀 تونل: 🟡 {self.url} وصل است ولی WARP ثبت نشده "
            f"(warp={self.warp or 'نامعلوم'}) — ترافیک از همان IP میزبان می‌رود، پس بلاک "
            "باقی می‌ماند"
        )


def _as_url(url: str) -> str:
    """A configured proxy, with a scheme — ``warp:1080`` is what people write."""
    return url if "://" in url else f"http://{url}"


def _trace_values(text: str) -> dict[str, str]:
    """Parse ``key=value`` lines of the trace endpoint (ignoring anything else)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and value.strip():
            values[key.strip()] = value.strip()
    return values


async def _tcp_probe(url: str, host: str, port: int, timeout: float) -> TunnelHealth:
    """The honest fallback: something is listening, and that is all we can say."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 — "cannot tell" is a valid answer
        return TunnelHealth(url=url, reachable=False, detail=f"{type(exc).__name__}: {exc}")
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return TunnelHealth(
        url=url, reachable=True, detail=f"{host}:{port} پذیرفت (warp قابل خواندن نبود)"
    )


async def _trace(proxy: str | None, timeout: float) -> tuple[int, str]:
    """The trace status and body, through ``proxy`` or directly when it is ``None``."""
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        async with session.get(TRACE_URL, proxy=proxy) as response:
            return response.status, await response.text()


async def direct_exit_ip(timeout: float = PROBE_TIMEOUT_S) -> str:
    """This host's *own* address, as Cloudflare sees it.

    The other half of the comparison a tunnel is for: an exit address equal to this
    one means the proxy is not changing the address YouTube sees — which is a
    different problem from a tunnel that is down, with a different fix. Empty when
    the answer could not be had (no outbound access, or a blocked trace endpoint),
    because "cannot tell" must not be reported as "the same address".
    """
    try:
        _, text = await _trace(None, timeout)
    except Exception:  # noqa: BLE001 — an unreachable trace endpoint is an answer
        return ""
    return _trace_values(text).get("ip", "")


async def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> TunnelHealth:
    """Ask the proxy where it goes. Never raises: an answer is always a diagnosis."""
    url = (url or "").strip()
    if not url:
        return TunnelHealth()
    parsed = urlparse(_as_url(url))
    host = parsed.hostname or ""
    port = parsed.port or DEFAULT_PROXY_PORT
    if not host:
        return TunnelHealth(url=url, reachable=False, detail="آدرس پروکسی خوانده نشد")
    if (parsed.scheme or "").lower() not in _DIRECT_SCHEMES:
        # `socks5://` needs aiohttp-socks, which is deliberately not a dependency of
        # the bot: the tunnel image serves HTTP on the same port.
        return await _tcp_probe(url, host, port, timeout)

    try:
        status, text = await _trace(url, timeout)
    except Exception as exc:  # noqa: BLE001 — the exception *is* the finding
        return TunnelHealth(url=url, reachable=False, detail=f"{type(exc).__name__}: {exc}")

    values = _trace_values(text)
    return TunnelHealth(
        url=url,
        reachable=True,
        traced=True,
        warp=values.get("warp", ""),
        exit_ip=values.get("ip", ""),
        detail=f"HTTP {status}",
    )


def for_ytdlp(tunnel: TunnelHealth) -> str:
    """The proxy yt-dlp should actually be handed.

    The tunnel when it answers, and an empty string (direct) when it does not —
    because a proxy that is down is worse than no proxy at all: the bot would then
    answer *every* link with a connection error, which is a failure the operator
    invented. ``warp=off`` is deliberately still handed over: it is no worse than
    going direct, and silently discarding a setting someone wrote is how an operator
    ends up debugging a tunnel that is not in the path.
    """
    return tunnel.url if tunnel.usable else ""


def boot_line(tunnel: TunnelHealth) -> str:
    """The one startup log line, whatever the answer was."""
    if not tunnel.configured:
        return "yt-dlp proxy: none (no tunnel configured)"
    if not tunnel.reachable:
        return f"yt-dlp tunnel {tunnel.url} is DOWN ({tunnel.detail}) — downloads run direct"
    if not tunnel.traced:
        return f"yt-dlp tunnel {tunnel.url} answers ({tunnel.detail})"
    where = f" from {tunnel.exit_ip}" if tunnel.exit_ip else ""
    return f"yt-dlp tunnel {tunnel.url}: warp={tunnel.warp or 'unknown'}{where}"


def startup_notice(tunnel: TunnelHealth) -> str | None:
    """What the admins must hear at boot, or ``None`` when nothing is wrong.

    Two cases, and they are not the same problem: a tunnel that is *down* means
    downloads left the route the operator chose, and one that answers without WARP
    means they are still on the address YouTube flagged. Both only ever appear once
    per boot (unlike ``helper_watch``, which pages on a timer), and both name one
    command — a boot notice that does not say what to do is just noise at 3am.
    """
    if not tunnel.configured or (tunnel.reachable and not tunnel.wrong_exit):
        # Healthy, or a tunnel whose exit cannot be read (SOCKS5): the second is not a
        # fault to page about, it is a limit of the question that was asked.
        return None
    if not tunnel.reachable:
        return (
            "🔀 <b>تونل yt-dlp بالا نیامد</b>\n"
            f"{tunnel.url} پاسخ نداد ({tunnel.detail}).\n"
            "دانلودها همین حالا مستقیم از IP این هاست می‌روند — اگر یوتیوب آن را فلگ "
            "کرده باشد، همان بلاک برمی‌گردد.\n"
            "قدم بعدی: <code>docker compose up -d warp</code> و بعد "
            "<code>/doctor</code>. (WARP چند ثانیه بعد از استارت ثبت می‌شود.)"
        )
    if tunnel.wrong_exit:
        return (
            "🔀 <b>تونل yt-dlp وصل است ولی WARP ثبت نشده</b>\n"
            f"{tunnel.url} — warp={tunnel.warp or 'نامعلوم'}.\n"
            "ترافیک از همان IP میزبان بیرون می‌رود، پس بلاک یوتیوب باقی می‌ماند.\n"
            "قدم بعدی: <code>docker compose logs warp</code> و بعد "
            "<code>docker compose restart warp</code>."
        )
    return None


async def probe_with_retries(
    url: str,
    *,
    attempts: int = 5,
    delay_s: float = 2.0,
    timeout: float = PROBE_TIMEOUT_S,
) -> TunnelHealth:
    """Probe until it answers — WARP registers a few seconds *after* it starts.

    Bounded on purpose: the bot's boot waits for this, and a tunnel that is not up is
    handled (direct, plus a notice), not waited on forever. An empty setting is not a
    probe at all — it is "this deployment does not use a tunnel".
    """
    if not (url or "").strip():
        return TunnelHealth()
    health = await probe(url, timeout=timeout)
    for _ in range(max(0, attempts - 1)):
        if health.reachable:
            return health
        await asyncio.sleep(delay_s)
        health = await probe(url, timeout=timeout)
    return health
