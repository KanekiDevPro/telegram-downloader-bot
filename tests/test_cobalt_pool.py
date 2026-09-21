"""The fallback *pool*: one blocked host, several addresses to try.

The embedded instance runs on this host's address, so when YouTube has flagged that
address the embedded fallback fails exactly like the primary engine did. A pool is
the fix — and the whole value of it is in the *ordering* rules pinned here: a node
that failed as a node is set aside on its own, a node that answered keeps the work,
and a link the instance had a real opinion about is never replayed three times.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

import aiohttp
import pytest

from services.cobalt import CobaltError, CobaltService

LOCAL = "http://cobalt:9000"
PUBLIC = "https://api.cobalt.example"
MIRROR = "https://mirror.example"


class FakeResponse:
    def __init__(self, *, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body

    async def json(self, **kwargs: Any) -> Any:
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class NodeSession:
    """A session that answers *per host* — the entire point of a pool.

    ``responses`` maps a netloc to the answers it gives, in order; an ``Exception``
    in the queue is raised instead of returned, which is how a dead node behaves
    (a refused connection, a DNS failure, a timeout).
    """

    def __init__(self, responses: dict[str, list[Any]]) -> None:
        # Keyed by *netloc* so a test can name the instance as it is configured.
        self._responses = {urlparse(url).netloc: list(items) for url, items in responses.items()}
        self.hosts: list[str] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        host = urlparse(url).netloc
        self.hosts.append(host)
        queue = self._responses.get(host)
        assert queue, f"no answer queued for {host!r}"
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def close(self) -> None:
        return None


def _stream(url: str = "https://cdn.example/v.mp4") -> FakeResponse:
    return FakeResponse(body={"status": "stream", "url": url})


def _dead() -> Exception:
    return aiohttp.ClientConnectionError("connection refused")


def _pool(session: NodeSession, *urls: str) -> CobaltService:
    return CobaltService(list(urls), session=session)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


async def test_the_next_node_answers_when_the_first_cannot_be_reached() -> None:
    """The failure this exists for: the embedded instance dies with the host's IP."""
    session = NodeSession({LOCAL: [_dead()], PUBLIC: [_stream()]})

    media = await _pool(session, LOCAL, PUBLIC).resolve("https://youtu.be/abc", "video")

    assert media.url == "https://cdn.example/v.mp4"
    assert session.hosts == ["cobalt:9000", "api.cobalt.example"]


async def test_the_node_that_answered_is_tried_first_next_time() -> None:
    """Failover is about one service, not the whole pool.

    An embedded instance without a YouTube session will fail for *every* YouTube
    link; asking it again first each time would pay a failed request per user.
    """
    session = NodeSession({LOCAL: [_dead()], PUBLIC: [_stream("https://cdn.example/1"), _stream("https://cdn.example/2")]})
    service = _pool(session, LOCAL, PUBLIC)

    await service.resolve("https://youtu.be/one", "video")
    await service.resolve("https://youtu.be/two", "video")

    assert session.hosts == ["cobalt:9000", "api.cobalt.example", "api.cobalt.example"]


async def test_a_node_that_failed_is_set_aside_on_its_own() -> None:
    """One bad node must not quarantine the pool: the others are still real options."""
    session = NodeSession({LOCAL: [_dead()], PUBLIC: [_stream()]})
    service = _pool(session, LOCAL, PUBLIC)

    await service.resolve("https://youtu.be/abc", "video")

    states = service.node_states()
    assert [(state.url, state.quarantined, state.active) for state in states] == [
        (LOCAL, True, False),
        (PUBLIC, False, True),
    ]
    assert "connection refused" in states[0].reason
    assert not service.quarantined, "the pool itself is still available"
    assert service.available


async def test_the_pool_is_out_only_when_every_node_is() -> None:
    """`quarantined` answers \"is there any net left?\", not \"did node one fail?\"."""
    session = NodeSession({LOCAL: [_dead(), _dead()], PUBLIC: [_dead(), _dead()]})
    service = _pool(session, LOCAL, PUBLIC)

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/abc", "video")

    assert caught.value.instance
    assert "روی هر 2 نمونه امتحان شد" in caught.value.message
    assert service.quarantined and not service.available


async def test_a_link_the_instance_answered_about_is_not_replayed_on_every_node() -> None:
    """A deleted video is deleted everywhere; rotating would triple the work and
    hide the real cause behind a pool summary."""
    session = NodeSession(
        {
            LOCAL: [
                FakeResponse(status=404, body={}),  # retired v7 path
                FakeResponse(
                    status=200,
                    body={
                        "status": "error",
                        "error": {"code": "error.api.content.video.unavailable"},
                    },
                ),
            ],
            PUBLIC: [_stream()],
        }
    )
    service = _pool(session, LOCAL, PUBLIC)

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/private", "video")

    assert caught.value.upstream == "error.api.content.video.unavailable"
    assert session.hosts == ["cobalt:9000", "cobalt:9000"], "node two was never asked"
    assert not service.quarantined, "and the node stays usable for other links"


async def test_a_node_that_wants_a_key_hands_the_link_to_the_next_one() -> None:
    """The public instance refusing anonymous callers is *why* it is last, not a
    reason the pool gives up: the operator's mirror may answer happily."""
    session = NodeSession(
        {
            LOCAL: [
                _dead(),
            ],
            PUBLIC: [
                FakeResponse(
                    status=400,
                    body={"status": "error", "error": {"code": "error.api.auth.jwt.missing"}},
                )
            ],
            MIRROR: [_stream()],
        }
    )
    service = _pool(session, LOCAL, PUBLIC, MIRROR)

    media = await service.resolve("https://youtu.be/abc", "video")

    assert media.url == "https://cdn.example/v.mp4"
    assert session.hosts[-1] == "mirror.example"
    reason = {state.url: state.reason for state in service.node_states()}[PUBLIC]
    assert "error.api.auth.jwt.missing" in reason


# ---------------------------------------------------------------------------
# Configuration and reporting
# ---------------------------------------------------------------------------


def test_the_primary_is_still_the_address_every_report_names() -> None:
    """`base_url` is what `/doctor`, the cookie watcher and the logs read."""
    service = _pool(NodeSession({}), LOCAL, PUBLIC)

    assert service.base_url == LOCAL
    assert service.base_urls == (LOCAL, PUBLIC)
    assert service.pool_label() == f"{LOCAL} (+1 more)"


def test_a_single_node_reads_exactly_as_before() -> None:
    service = _pool(NodeSession({}), LOCAL)

    assert service.base_url == LOCAL
    assert service.pool_label() == LOCAL
    assert len(service.node_states()) == 1
    assert not service.quarantined


def test_no_addresses_at_all_is_off_not_broken() -> None:
    service = CobaltService("", session=NodeSession({}))  # type: ignore[arg-type]

    assert not service.enabled
    assert service.node_states() == ()
    assert service.pool_label() == ""


async def test_a_disabled_pool_says_so_instead_of_dialling_nothing() -> None:
    service = CobaltService([], session=NodeSession({}))  # type: ignore[arg-type]

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/abc", "video")

    assert caught.value.code == "DISABLED"


def test_duplicate_addresses_are_collapsed() -> None:
    """The same instance twice is not a second attempt, it is a wasted round trip."""
    service = _pool(NodeSession({}), LOCAL, f"{LOCAL}/", "http://cobalt:9000")

    assert service.base_urls == (LOCAL,)


def test_the_dialect_reported_is_the_ones_that_answered() -> None:
    """`v7` and `v10` are different fixes for whoever reads the report."""
    service = _pool(NodeSession({}), LOCAL, PUBLIC)

    node = service._nodes[1]
    node.endpoint_index = 0
    service._active_url = PUBLIC

    assert service.dialect == "v7"


async def test_a_transport_failure_is_not_an_answer_from_the_second_node() -> None:
    """Both dead: the error must be the *last* real cause, not a fabricated one."""
    session = NodeSession({LOCAL: [_dead()], PUBLIC: [asyncio.TimeoutError()]})
    service = _pool(session, LOCAL, PUBLIC)

    with pytest.raises(CobaltError) as caught:
        await service.resolve("https://youtu.be/abc", "video")

    assert caught.value.code == "TIMEOUT"
    assert [state.quarantined for state in service.node_states()] == [True, True]


@pytest.mark.parametrize("urls", [(), ("",)])
def test_an_empty_pool_has_no_node_states(urls: Iterable[str]) -> None:
    service = CobaltService(list(urls), session=NodeSession({}))  # type: ignore[arg-type]

    assert service.node_states() == ()
