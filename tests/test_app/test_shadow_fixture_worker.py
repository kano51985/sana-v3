from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from sana.app.shadow_fixture_worker import (
    FIXTURE_HOST,
    ShadowFixtureContentFetcher,
    ShadowFixtureModelGateway,
    ShadowFixtureSearchProvider,
)
from sana.modules.content.domain import FetchRequest, FetchStatus
from sana.modules.discovery.domain import DiscoveryQuery
from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelMessage,
    ModelRole,
)
from sana.modules.search_planning.planner import IntentParser
from sana.modules.search_planning.policy import SearchPlanningPolicy
from sana.modules.shared.clock import FrozenClock


NOW = datetime(2026, 8, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fixture_planner_marks_unanswerable_requests_without_provider_calls() -> None:
    gateway = ShadowFixtureModelGateway()
    budget = ModelCallBudget(2, 1_000)
    result = await gateway.generate(
        ModelRole.PLANNER,
        (
            ModelMessage(MessageRole.SYSTEM, "fixture system"),
            ModelMessage(
                MessageRole.USER,
                "Current request:\nFind private parameter weights for an unreleased model",
            ),
        ),
        deadline=NOW + timedelta(minutes=1),
        budget=budget,
        parser=IntentParser(SearchPlanningPolicy()),
    )

    assert result.provider_calls == 0
    assert result.prompt_tokens == result.completion_tokens == 0
    assert result.parsed.entity == "shadow-no-answer"
    assert budget.used_calls == 0


@pytest.mark.asyncio
async def test_fixture_search_and_fetch_are_network_free_and_deterministic() -> None:
    clock = FrozenClock(NOW)
    provider = ShadowFixtureSearchProvider(clock)
    response = await provider.search(
        DiscoveryQuery("q:1", "Python latest stable version official", "en"),
        timeout_seconds=1,
    )

    assert response.ok is True
    assert len(response.hits) == 1
    assert FIXTURE_HOST in response.hits[0].url
    fetcher = ShadowFixtureContentFetcher(clock)
    artifact = await fetcher.fetch(
        FetchRequest(response.hits[0].url, NOW + timedelta(minutes=1))
    )
    await fetcher.aclose()

    assert artifact.status is FetchStatus.SUCCEEDED
    assert b"Python latest stable version official" in artifact.body


@pytest.mark.asyncio
async def test_fixture_verifier_uses_exact_supplied_ids_and_quote() -> None:
    class Parser:
        def parse(self, text: str):
            return json.loads(text)["verdicts"]

    gateway = ShadowFixtureModelGateway()
    result = await gateway.generate(
        ModelRole.VERIFIER,
        (
            ModelMessage(MessageRole.SYSTEM, "fixture system"),
            ModelMessage(
                MessageRole.USER,
                json.dumps(
                    {
                        "candidates": [
                            {
                                "fact_id": "11111111-1111-1111-1111-111111111111",
                                "candidate_id": "22222222-2222-2222-2222-222222222222",
                                "quote": "exact fixture quote",
                            }
                        ]
                    }
                ),
            ),
        ),
        parser=Parser(),
    )

    assert result.provider_calls == 0
    assert result.parsed[0]["quote"] == "exact fixture quote"
