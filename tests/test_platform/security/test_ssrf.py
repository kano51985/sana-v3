from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sana.modules.content.domain import FetchRequest, FetchStatus
from sana.modules.shared.clock import FrozenClock, SystemClock
from sana.modules.shared.errors import ErrorCategory
from sana.platform.fetch.http_fetcher import HttpContentFetcher
from sana.platform.security.ssrf import SSRFBlocked, SSRFGuard


PUBLIC_IP = "93.184.216.34"
NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeResolver:
    def __init__(self, answers: dict[str, tuple[str, ...] | list[tuple[str, ...]]]) -> None:
        self.answers = answers

    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        answer = self.answers[host]
        if isinstance(answer, list):
            return answer.pop(0)
        return answer


class FakeNetworkStream:
    def __init__(self, peer: str) -> None:
        self.peer = peer

    def get_extra_info(self, name: str):
        if name in {"server_addr", "peername"}:
            return (self.peer, 443)
        return None


class SlowResolver:
    async def resolve(self, host: str, port: int) -> tuple[str, ...]:
        import asyncio

        await asyncio.sleep(10)
        return (PUBLIC_IP,)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://localhost/",
        "http://service.internal/",
        "file:///etc/passwd",
        "http://user:secret@example.com/",
        "http://example.com:22/",
    ],
)
async def test_private_internal_and_unsafe_targets_are_blocked(url: str) -> None:
    guard = SSRFGuard(FakeResolver({"example.com": (PUBLIC_IP,)}))

    with pytest.raises(SSRFBlocked):
        await guard.resolve_and_validate(url)


async def test_every_dns_answer_must_be_public() -> None:
    guard = SSRFGuard(
        FakeResolver({"mixed.example": (PUBLIC_IP, "10.0.0.1")})
    )

    with pytest.raises(SSRFBlocked):
        await guard.resolve_and_validate("https://mixed.example/")


async def test_public_target_is_allowed() -> None:
    target = await SSRFGuard(
        FakeResolver({"public.example": (PUBLIC_IP,)})
    ).resolve_and_validate("https://public.example/path")

    assert target.host == "public.example"
    assert tuple(map(str, target.addresses)) == (PUBLIC_IP,)


async def test_fetch_deadline_also_bounds_dns_resolution() -> None:
    clock = SystemClock()
    fetcher = HttpContentFetcher(SSRFGuard(SlowResolver()), clock)

    artifact = await fetcher.fetch(
        FetchRequest(
            "https://slow.example/",
            clock.now() + timedelta(milliseconds=10),
        )
    )
    await fetcher.aclose()

    assert artifact.status is FetchStatus.FAILED
    assert artifact.error is not None
    assert artifact.error.code == "fetch_deadline_exceeded"


async def test_redirect_to_private_target_is_blocked_before_second_request() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/secret"},
            extensions={"network_stream": FakeNetworkStream(PUBLIC_IP)},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP,)})),
        FrozenClock(NOW),
        client=client,
    )
    artifact = await fetcher.fetch(
        FetchRequest("https://public.example/start", NOW + timedelta(seconds=5))
    )
    await client.aclose()

    assert artifact.status is FetchStatus.BLOCKED
    assert artifact.error is not None and artifact.error.code == "ssrf_blocked"
    assert requests == ["https://public.example/start"]


async def test_connected_private_peer_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"should not be trusted",
            extensions={"network_stream": FakeNetworkStream("10.0.0.2")},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP,)})),
        FrozenClock(NOW),
        client=client,
    )
    artifact = await fetcher.fetch(
        FetchRequest("https://public.example/", NOW + timedelta(seconds=5))
    )
    await client.aclose()

    assert artifact.status is FetchStatus.BLOCKED


async def test_dns_rebinding_is_detected_after_connection() -> None:
    resolver = FakeResolver(
        {"public.example": [(PUBLIC_IP,), ("10.0.0.3",)]}
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"untrusted",
                extensions={"network_stream": FakeNetworkStream(PUBLIC_IP)},
            )
        )
    )
    fetcher = HttpContentFetcher(SSRFGuard(resolver), FrozenClock(NOW), client=client)
    artifact = await fetcher.fetch(
        FetchRequest("https://public.example/", NOW + timedelta(seconds=5))
    )
    await client.aclose()

    assert artifact.status is FetchStatus.BLOCKED


async def test_missing_connected_peer_information_fails_closed() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"transport hid its connected peer",
            )
        )
    )
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP,)})),
        FrozenClock(NOW),
        client=client,
    )
    artifact = await fetcher.fetch(
        FetchRequest("https://public.example/", NOW + timedelta(seconds=5))
    )
    await client.aclose()

    assert artifact.status is FetchStatus.BLOCKED
    assert artifact.error is not None and artifact.error.code == "ssrf_blocked"


async def test_declared_oversized_response_is_rejected() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/plain", "content-length": "101"},
                content=b"small",
                extensions={"network_stream": FakeNetworkStream(PUBLIC_IP)},
            )
        )
    )
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP, PUBLIC_IP)})),
        FrozenClock(NOW),
        client=client,
    )
    artifact = await fetcher.fetch(
        FetchRequest(
            "https://public.example/",
            NOW + timedelta(seconds=5),
            max_response_bytes=100,
        )
    )
    await client.aclose()

    assert artifact.status is FetchStatus.FAILED
    assert artifact.error is not None and artifact.error.code == "response_too_large"


async def test_successful_http_fetch_is_bounded_and_hashed() -> None:
    body = b"<html><body>verified</body></html>"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": str(len(body))},
                content=body,
                extensions={"network_stream": FakeNetworkStream(PUBLIC_IP)},
            )
        )
    )
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP, PUBLIC_IP)})),
        FrozenClock(NOW),
        client=client,
    )
    artifact = await fetcher.fetch(
        FetchRequest(
            "https://public.example/",
            NOW + timedelta(seconds=5),
            max_response_bytes=len(body),
        )
    )
    await client.aclose()

    assert artifact.status is FetchStatus.SUCCEEDED
    assert artifact.body == body
    assert artifact.content_hash is not None


async def test_csv_is_an_allowed_fetch_content_type() -> None:
    body = b"Value,Description\n404,Not Found\n"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/csv"},
                content=body,
                extensions={"network_stream": FakeNetworkStream(PUBLIC_IP)},
            )
        )
    )
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP, PUBLIC_IP)})),
        FrozenClock(NOW),
        client=client,
    )

    artifact = await fetcher.fetch(
        FetchRequest("https://public.example/status.csv", NOW + timedelta(seconds=5))
    )
    await client.aclose()

    assert artifact.status is FetchStatus.SUCCEEDED
    assert artifact.media_type == "text/csv"


async def test_public_source_403_is_a_content_failure_not_configuration() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403,
                headers={"content-type": "text/plain"},
                content=b"forbidden",
                extensions={"network_stream": FakeNetworkStream(PUBLIC_IP)},
            )
        )
    )
    fetcher = HttpContentFetcher(
        SSRFGuard(FakeResolver({"public.example": (PUBLIC_IP, PUBLIC_IP)})),
        FrozenClock(NOW),
        client=client,
    )

    artifact = await fetcher.fetch(
        FetchRequest("https://public.example/blocked", NOW + timedelta(seconds=5))
    )
    await client.aclose()

    assert artifact.status is FetchStatus.FAILED
    assert artifact.error is not None
    assert artifact.error.code == "fetch_http_403"
    assert artifact.error.category is ErrorCategory.CONTENT
