from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sana.app.production_worker import (
    ProductionWorkerSettings,
    create_handler,
)
from sana.app.search_operations import HeuristicIntentPlanner
from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.query_compiler import QueryCompiler


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_local_heuristic_planner_removes_conversation_filler_from_queries() -> None:
    planner = HeuristicIntentPlanner("search-v1")

    intent, calls = await planner.plan(
        "sana！我好久没碰 Apex Legends 啦，请告诉我最新版本和角色改动？",
        mode=SearchMode.RESEARCH,
        deadline=NOW,
        max_llm_calls=8,
    )
    queries = QueryCompiler().compile(intent, SearchMode.RESEARCH)

    assert intent.entity == "Apex Legends"
    assert calls == 0
    assert queries
    assert all("sana" not in query.text.casefold() for query in queries)
    assert all("我好久" not in query.text for query in queries)
    assert all("请告诉我" not in query.text for query in queries)


def test_production_rejects_offline_heuristic_planner() -> None:
    with pytest.raises(ValueError, match="model-backed planner"):
        ProductionWorkerSettings(
            environment="production",
            auth_mode="oidc",
            dev_auth_enabled=False,
            oidc_issuer="https://issuer.example",
            oidc_audience="sana",
            oidc_jwks_url="https://issuer.example/jwks",
            worker_planner_provider="heuristic",
        )


def test_model_backed_planner_requires_explicit_model_name() -> None:
    with pytest.raises(ValueError, match="model name"):
        ProductionWorkerSettings(
            worker_planner_provider="deepseek",
            worker_planner_model="",
        )


def test_worker_heartbeat_must_fit_inside_lease() -> None:
    with pytest.raises(ValueError, match="shorter than its lease"):
        ProductionWorkerSettings(
            worker_heartbeat_seconds=10,
            worker_lease_seconds=10,
        )


def test_worker_defaults_recover_well_inside_the_fast_deadline() -> None:
    settings = ProductionWorkerSettings()

    assert settings.worker_heartbeat_seconds == 2.0
    assert settings.worker_lease_seconds == 6.0
    assert settings.worker_lease_seconds < 15.0


def test_production_factory_is_lazy_and_does_not_open_async_resources() -> None:
    handler = create_handler()

    assert callable(handler)
    assert handler._runtime is None
    handler.close()
