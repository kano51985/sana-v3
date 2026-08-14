import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.discovery.domain import (
    DiscoveryQuery,
    ProviderMetrics,
    ProviderResponse,
    SearchHit,
)
from sana.modules.discovery.service import DiscoveryConcurrency, DiscoveryService
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import ErrorCategory, TypedError


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeProvider:
    def __init__(self, name, *, error=None, delay=0.0) -> None:
        self.name = name
        self.error = error
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def search(self, query, *, timeout_seconds):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error:
                raise self.error
            hit = SearchHit(
                self.name,
                query.key,
                1,
                f"https://example.com/{query.key}",
                f"https://example.com/{query.key}",
                query.text,
                "snippet",
                1.0,
            )
            return ProviderResponse(
                self.name,
                query.key,
                (hit,),
                ProviderMetrics(1, raw_hit_count=1),
            )
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_provider_success_and_failure_remain_separate_per_query() -> None:
    good = FakeProvider("good")
    bad = FakeProvider(
        "bad",
        error=TypedError(
            ErrorCategory.TRANSIENT,
            "provider_down",
            "down",
            retryable=True,
        ),
    )
    queries = (
        DiscoveryQuery("q1", "first", "en"),
        DiscoveryQuery("q2", "second", "en"),
    )
    service = DiscoveryService(
        {"good": good, "bad": bad},
        FrozenClock(NOW),
    )

    responses = await service.discover(
        uuid4(),
        queries,
        ("good", "bad"),
        deadline=NOW + timedelta(seconds=2),
    )

    assert len(responses) == 4
    assert {(response.provider, response.query_key) for response in responses} == {
        ("good", "q1"),
        ("bad", "q1"),
        ("good", "q2"),
        ("bad", "q2"),
    }
    assert sum(response.ok for response in responses) == 2
    assert not hasattr(good, "last_trace")


@pytest.mark.asyncio
async def test_provider_concurrency_is_bounded_without_nested_thread_pools() -> None:
    provider = FakeProvider("bounded", delay=0.02)
    service = DiscoveryService(
        {"bounded": provider},
        FrozenClock(NOW),
        concurrency=DiscoveryConcurrency(
            global_limit=10,
            tenant_limit=10,
            provider_limit=2,
        ),
    )
    queries = tuple(DiscoveryQuery(f"q{i}", f"query {i}", "en") for i in range(6))

    responses = await service.discover(
        uuid4(),
        queries,
        ("bounded",),
        deadline=NOW + timedelta(seconds=2),
    )

    assert all(response.ok for response in responses)
    assert provider.max_active == 2


@pytest.mark.asyncio
async def test_expired_deadline_skips_provider_call() -> None:
    provider = FakeProvider("unused")
    service = DiscoveryService({"unused": provider}, FrozenClock(NOW))

    responses = await service.discover(
        uuid4(),
        (DiscoveryQuery("q", "query", "en"),),
        ("unused",),
        deadline=NOW,
    )

    assert responses[0].error.category is ErrorCategory.BUDGET
    assert provider.max_active == 0
