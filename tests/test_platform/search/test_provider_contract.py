from datetime import datetime, timedelta, timezone

import httpx
import pytest

from sana.modules.discovery.domain import DiscoveryQuery
from sana.modules.shared.clock import FrozenClock
from sana.platform.search.bing_rss import BingRssProvider
from sana.platform.search.circuit_breaker import CircuitBreaker, CircuitState
from sana.platform.search.direct_source import DirectSourceProvider
from sana.platform.search.searxng import SearxngProvider


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_direct_source_is_stateless_and_canonicalizes_urls() -> None:
    provider = DirectSourceProvider()
    response = await provider.search(
        DiscoveryQuery(
            "q1",
            "official",
            "en",
            direct_urls=("https://Example.com/path?utm_source=test&x=1#fragment",),
        ),
        timeout_seconds=1,
    )

    assert response.ok
    assert response.hits[0].canonical_url == "https://example.com/path?x=1"
    assert not hasattr(provider, "last_trace")


@pytest.mark.asyncio
async def test_bing_rss_contract_returns_call_local_metrics_and_hits() -> None:
    rss = b"""<?xml version='1.0'?>
    <rss><channel><item><title>Result</title>
    <link>https://example.com/article?utm_campaign=x</link>
    <description><![CDATA[<b>Useful</b> snippet]]></description>
    <pubDate>Fri, 14 Aug 2026 08:00:00 GMT</pubDate>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "rss"
        return httpx.Response(200, content=rss)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await BingRssProvider(client).search(
            DiscoveryQuery("q1", "Apex patch", "en"),
            timeout_seconds=2,
        )
    finally:
        await client.aclose()

    assert response.ok
    assert response.metrics.response_bytes == len(rss)
    assert response.hits[0].snippet == "Useful snippet"
    assert response.hits[0].canonical_url == "https://example.com/article"


@pytest.mark.asyncio
async def test_searxng_contract_filters_invalid_result_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "file:///etc/passwd", "title": "bad"},
                    {
                        "url": "https://example.org/good",
                        "title": "good",
                        "content": "snippet",
                        "score": 0.7,
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        response = await SearxngProvider(
            client,
            base_url="https://search.example",
        ).search(
            DiscoveryQuery("q1", "query", "en"),
            timeout_seconds=2,
        )
    finally:
        await client.aclose()

    assert response.ok
    assert response.metrics.raw_hit_count == 2
    assert [hit.title for hit in response.hits] == ["good"]


def test_circuit_breaker_allows_one_half_open_probe() -> None:
    clock = FrozenClock(NOW)
    breaker = CircuitBreaker(clock, failure_threshold=2, recovery_seconds=10)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allow_request()

    clock.advance(timedelta(seconds=10))
    assert breaker.allow_request()
    assert breaker.state is CircuitState.HALF_OPEN
    assert not breaker.allow_request()

    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow_request()
