"""The proxy route, proven end to end instead of asserted.

`YTDLP_PROXY` is the only fix for an IP-level block, so the question that matters is
not "is the setting read?" but "does the traffic actually leave through it?" — for
the two engines that fetch bytes, and knowing that the browser-based session server
has no proxy knob at all.

Nothing here touches the internet: a real HTTP proxy (absolute-URI forwarding, and
CONNECT for completeness) runs in-process in front of real origin servers, records
every request line it forwards, and the engines are pointed at it. A test that only
checked `opts["proxy"]` would pass while the engine quietly bypassed the proxy —
which is precisely the failure an operator would chase for hours.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from services.cobalt import CobaltMedia, CobaltService
from services.extractor import ExtractorService

PAGE = (
    "<html><head><title>Proxy Probe Clip</title></head><body>"
    '<video controls><source src="/clip.mp4" type="video/mp4"></video>'
    "</body></html>"
)


# ---------------------------------------------------------------------------
# A real proxy, and real origins to put behind it
# ---------------------------------------------------------------------------


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes one way until EOF, then half-close (so the peer sees the end)."""
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        with_suppress = getattr(writer, "write_eof", None)
        if with_suppress is not None:
            try:
                writer.write_eof()
            except (OSError, RuntimeError):
                pass


def _target_of(target: bytes, host_header: bytes) -> tuple[str, int]:
    """Where a proxied request has to be sent: absolute URI, else the Host header."""
    text = target.decode("latin-1")
    if "://" in text:
        parsed = urlparse(text)
        return parsed.hostname or "", parsed.port or 80
    host = host_header.decode("latin-1").partition(":")[2].strip() or "127.0.0.1"
    host, _, port = host.partition(":")
    return host, int(port or 80)


class RecordingProxy:
    """A minimal forward proxy that remembers what it was asked to reach.

    Deliberately a *real* socket server: the point is to observe the engines'
    traffic, and an in-process fake client session could not tell whether aiohttp
    (or yt-dlp's own opener) decided to ignore the proxy.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def hosts(self) -> set[str]:
        return {request.split()[1].partition("//")[2].partition("/")[0] for request in self.requests}

    async def __aenter__(self) -> RecordingProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        lines = head.split(b"\r\n")
        method, target, _version = lines[0].split()
        self.requests.append(lines[0].decode("latin-1"))
        host_header = next(
            (line for line in lines[1:] if line.lower().startswith(b"host:")), b""
        )
        try:
            host, port = _target_of(target, host_header)
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        if method == b"CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            parsed = urlparse(target.decode("latin-1"))
            origin_form = parsed.path or "/"
            if parsed.query:
                origin_form += f"?{parsed.query}"
            upstream_writer.write(
                f"{method.decode()} {origin_form} HTTP/1.1\r\n".encode()
                + b"\r\n".join(lines[1:])
                + b"\r\n\r\n"
            )
            await upstream_writer.drain()

        await asyncio.gather(
            _pump(reader, upstream_writer),
            _pump(upstream_reader, writer),
        )
        writer.close()


class _Origin(BaseHTTPRequestHandler):
    """Serves one configurable body, and closes after every response."""

    protocol_version = "HTTP/1.0"
    routes: dict[str, tuple[str, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 — http.server's own API
        content_type, body = self.routes.get(self.path, ("text/plain", b"missing"))
        self.send_response(200 if self.path in self.routes else 404)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Stay quiet: the proxy is what this test listens to."""


@pytest.fixture
def origin() -> Iterator[tuple[str, int]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        _Origin.routes = {}


# ---------------------------------------------------------------------------
# The fallback engine's transfer: the file must come through the proxy
# ---------------------------------------------------------------------------


async def test_the_fallback_download_goes_through_the_proxy(
    origin: tuple[str, int], tmp_path: Path
) -> None:
    host, port = origin
    _Origin.routes = {"/file.bin": ("application/octet-stream", b"x" * 4096)}

    async with RecordingProxy() as proxy:
        client = CobaltService("http://127.0.0.1:1", proxy=proxy.url, download_timeout_s=15)
        try:
            path = await client.download(
                CobaltMedia(url=f"http://{host}:{port}/file.bin"), tmp_path / "job", max_bytes=8192
            )
        finally:
            await client.close()

    assert path.read_bytes() == b"x" * 4096
    assert proxy.requests, "the proxy saw nothing — the download bypassed it"
    assert proxy.hosts == {f"{host}:{port}"}


async def test_a_download_without_a_proxy_never_touches_it(
    origin: tuple[str, int], tmp_path: Path
) -> None:
    """The control: with no proxy configured the same call goes direct."""
    host, port = origin
    _Origin.routes = {"/file.bin": ("application/octet-stream", b"y" * 512)}

    async with RecordingProxy() as proxy:
        client = CobaltService("http://127.0.0.1:1", download_timeout_s=15)
        try:
            path = await client.download(
                CobaltMedia(url=f"http://{host}:{port}/file.bin"), tmp_path / "job", max_bytes=8192
            )
        finally:
            await client.close()

    assert path.read_bytes() == b"y" * 512
    assert proxy.requests == []


# ---------------------------------------------------------------------------
# The primary engine: yt-dlp's own traffic must leave through it too
# ---------------------------------------------------------------------------


async def test_ytdlp_downloads_the_page_and_the_media_through_the_proxy(
    origin: tuple[str, int], tmp_path: Path
) -> None:
    """A whole download, not a setting: page + media, both seen by the proxy.

    The page is real HTML with a real `<source>`, so yt-dlp walks the same path it
    would for any site — and every request it makes has to arrive at the proxy, or a
    blocked host stays blocked while the log insists a proxy is configured.
    """
    host, port = origin
    _Origin.routes = {
        "/index.html": ("text/html; charset=utf-8", PAGE.encode()),
        "/clip.mp4": ("video/mp4", b"\x00" * 2048),
    }

    async with RecordingProxy() as proxy:
        extractor = ExtractorService(
            tmp_path, proxy=proxy.url, js_runtime="none", cookie_file=None
        )
        result = await extractor.download(f"http://{host}:{port}/index.html", "video")

    # yt-dlp may decorate the title ("… (1)") — what matters here is that the page
    # it read is ours, and that the media arrived through the proxy.
    assert "Proxy Probe Clip" in result.info.title
    assert result.file_path.read_bytes() == b"\x00" * 2048
    assert proxy.hosts == {f"{host}:{port}"}
    fetched = [request for request in proxy.requests if "/clip.mp4" in request]
    assert fetched, f"media was not fetched through the proxy: {proxy.requests}"
    assert len(fetched) == 1, f"the media was fetched more than once: {proxy.requests}"


def test_the_engine_gives_ytdlp_the_proxy_in_the_option_it_reads(tmp_path: Path) -> None:
    """`--proxy` has one spelling in yt-dlp; a different one would be inert."""
    extractor = ExtractorService(tmp_path, proxy="socks5://user:pw@127.0.0.1:1080")

    assert extractor._base_opts(extract_only=True)["proxy"] == "socks5://user:pw@127.0.0.1:1080"
    assert extractor._base_opts(extract_only=False)["proxy"] == "socks5://user:pw@127.0.0.1:1080"


def test_an_engine_without_a_proxy_says_so(tmp_path: Path) -> None:
    extractor = ExtractorService(tmp_path)

    assert extractor.using_proxy is False
    assert "proxy" not in extractor._base_opts(extract_only=True)


# ---------------------------------------------------------------------------
# What a proxy cannot reach: the browser-based helper
# ---------------------------------------------------------------------------


def test_the_session_generator_has_no_proxy_setting_to_wire() -> None:
    """The one route a proxy does not fix, stated in code rather than in prose.

    The generator drives a real Chromium from the host's own address (that is the
    whole point of a *trusted* session — its token is bound to the IP that created
    it), and its image takes no proxy variable. So a proxy cannot move it: if a host
    needs a proxy for YouTube, the token route is unavailable and the login jar
    (exported through that same proxy) is what remains.
    """
    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    generator = compose.split("yt-session-generator:", 1)[1].split("\n  telegram-api:", 1)[0]

    assert "PROXY" not in generator.upper()
