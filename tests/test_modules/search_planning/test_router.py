import json
from pathlib import Path

import pytest

from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.router import AutomaticModeRouter
from sana.modules.orchestration.domain import RoutingDecision


FIXTURE = Path(__file__).parents[2] / "fixtures" / "evals" / "apex_multi_fact.json"


def test_apex_multi_fact_request_routes_directly_to_research() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))

    decision = AutomaticModeRouter("search-v11").route(case["user_message"])

    assert decision.mode is SearchMode.RESEARCH
    assert "three_or_more_required_facts" in decision.reason_codes
    assert "fresh_multi_fact" in decision.reason_codes
    assert decision.policy_version == "search-v11"


def test_simple_single_fact_stays_fast() -> None:
    decision = AutomaticModeRouter("search-v11").route("Apex Legends 是哪一年发布的？")
    assert decision.mode is SearchMode.FAST


def test_high_consequence_request_routes_research_for_cross_check() -> None:
    decision = AutomaticModeRouter("search-v11").route("这个医疗诊断结论可信吗？")
    assert decision.mode is SearchMode.RESEARCH
    assert "high_consequence_cross_check" in decision.reason_codes


def test_enumerated_and_cross_check_requests_route_to_research() -> None:
    router = AutomaticModeRouter("search-v11")

    enumerated = router.route("列出四种 Git 对象类型，并分别说明每种对象的用途。")
    cross_check = router.route(
        "Find and cross-check private model weights using multiple sources."
    )

    assert enumerated.mode is SearchMode.RESEARCH
    assert "enumerated_multi_fact" in enumerated.reason_codes
    assert cross_check.mode is SearchMode.RESEARCH
    assert "explicit_cross_check" in cross_check.reason_codes


@pytest.mark.parametrize(
    "message",
    [
        "Explain the three properties in the CAP theorem.",
        "解释数据库 ACID 四项性质。",
        "研究 PostgreSQL 当前仍受支持的主版本。",
    ],
)
def test_semantic_research_requests_route_without_model_boundary(message: str) -> None:
    decision = AutomaticModeRouter("search-v11").route(message)

    assert decision.mode is SearchMode.RESEARCH


@pytest.mark.parametrize(
    "message",
    [
        "What are the three lowercase JSON literals?",
        "JSON 标准规定的三个小写字面量是什么？请原样列出并引用标准来源。",
    ],
)
def test_single_source_enumeration_can_remain_fast(message: str) -> None:
    decision = AutomaticModeRouter("search-v11").route(message)

    assert decision.mode is SearchMode.FAST


class CountingBoundaryClassifier:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, message: str) -> RoutingDecision:
        self.calls += 1
        return RoutingDecision(SearchMode.RESEARCH, ("model_boundary",), "search-v11", 0.75)


async def test_boundary_classifier_is_called_at_most_once_for_ambiguous_text() -> None:
    classifier = CountingBoundaryClassifier()
    decision = await AutomaticModeRouter("search-v11").route_with_boundary_classifier(
        "它现在怎么样？",
        classifier,
    )

    assert decision.mode is SearchMode.RESEARCH
    assert classifier.calls == 1
