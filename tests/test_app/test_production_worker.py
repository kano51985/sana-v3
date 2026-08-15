from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sana.app.production_worker import (
    ProductionWorkerSettings,
    create_handler,
)
from sana.app.search_operations import HeuristicIntentPlanner, ModelIntentPlanner
from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.query_compiler import QueryCompiler
from sana.modules.shared.errors import ErrorCategory, TypedError


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FailingSearchPlanner:
    async def plan(self, *args, **kwargs):
        raise TypedError(
            ErrorCategory.TRANSIENT,
            "model_network_failure",
            "temporary provider failure",
            retryable=True,
        )


@pytest.mark.asyncio
async def test_local_heuristic_planner_removes_conversation_filler_from_queries() -> None:
    planner = HeuristicIntentPlanner("search-v1")

    planning = await planner.plan(
        "sana！我好久没碰 Apex Legends 啦，请告诉我最新版本和角色改动？",
        mode=SearchMode.RESEARCH,
        deadline=NOW,
        max_llm_calls=8,
    )
    queries = QueryCompiler().compile(planning.intent, SearchMode.RESEARCH)

    assert planning.intent.entity == "Apex Legends"
    assert planning.llm_calls == 0
    assert planning.degraded is False
    assert queries
    assert all("sana" not in query.text.casefold() for query in queries)
    assert all("我好久" not in query.text for query in queries)
    assert all("请告诉我" not in query.text for query in queries)


@pytest.mark.asyncio
async def test_model_planner_failure_uses_explicit_degraded_local_plan() -> None:
    planning = await ModelIntentPlanner(FailingSearchPlanner()).plan(
        "Python 当前稳定版本是什么？",
        mode=SearchMode.FAST,
        deadline=NOW,
        max_llm_calls=4,
    )

    assert planning.degraded is True
    assert planning.intent.entity == "Python"
    assert planning.intent.facts


def test_production_allows_explicit_deterministic_rollback() -> None:
    settings = ProductionWorkerSettings(
        environment="production",
        auth_mode="oidc",
        dev_auth_enabled=False,
        oidc_issuer="https://issuer.example",
        oidc_audience="sana",
        oidc_jwks_url="https://issuer.example/jwks",
        worker_model_pipeline_enabled=False,
    )

    assert settings.worker_model_pipeline_enabled is False


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("worker_planner_model", "planner"),
        ("worker_verifier_model", "verifier"),
        ("worker_synthesizer_model", "synthesizer"),
    ),
)
def test_enabled_model_pipeline_requires_every_role_model(
    field: str,
    message: str,
) -> None:
    values = {field: ""}
    with pytest.raises(ValueError, match=message):
        ProductionWorkerSettings(
            worker_model_pipeline_enabled=True,
            **values,
        )


def test_model_pipeline_defaults_are_safe_and_disabled() -> None:
    settings = ProductionWorkerSettings()

    assert settings.worker_model_pipeline_enabled is False
    assert settings.worker_planner_provider == "deepseek"
    assert settings.worker_verifier_provider == "deepseek"
    assert settings.worker_synthesizer_provider == "deepseek"
    assert settings.worker_planner_model == "deepseek-v4-flash"
    assert settings.worker_verifier_model == "deepseek-v4-flash"
    assert settings.worker_synthesizer_model == "deepseek-v4-flash"
    assert settings.worker_deepseek_base_url == "https://api.deepseek.com"
    assert settings.worker_model_thinking == "disabled"
    assert settings.worker_model_output_format == "json_object"
    assert settings.worker_live_eval_max_runs == 20
    assert settings.discovery_provider_names == ("direct", "bing_rss")


def test_enabled_pipeline_requires_one_shared_provider() -> None:
    with pytest.raises(ValueError, match="one shared provider"):
        ProductionWorkerSettings(
            worker_model_pipeline_enabled=True,
            worker_verifier_provider="openai",
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
