import json
from pathlib import Path

import pytest

from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.router import AutomaticModeRouter
from sana.modules.orchestration.domain import RoutingDecision


FIXTURE = Path(__file__).parents[2] / "fixtures" / "evals" / "apex_multi_fact.json"


def test_apex_multi_fact_request_routes_directly_to_research() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))

    decision = AutomaticModeRouter("search-v12").route(case["user_message"])

    assert decision.mode is SearchMode.RESEARCH
    assert "three_or_more_required_facts" in decision.reason_codes
    assert "fresh_multi_fact" in decision.reason_codes
    assert decision.policy_version == "search-v12"


def test_simple_single_fact_stays_fast() -> None:
    decision = AutomaticModeRouter("search-v12").route("Apex Legends 是哪一年发布的？")
    assert decision.mode is SearchMode.FAST


def test_high_consequence_request_routes_research_for_cross_check() -> None:
    decision = AutomaticModeRouter("search-v12").route("这个医疗诊断结论可信吗？")
    assert decision.mode is SearchMode.RESEARCH
    assert "high_consequence_cross_check" in decision.reason_codes


def test_enumerated_and_cross_check_requests_route_to_research() -> None:
    router = AutomaticModeRouter("search-v12")

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
        (
            "Explain Git's three commonly described file states and how the "
            "working tree, staging area, and Git directory relate to them."
        ),
        "解释数据库 ACID 四项性质。",
        "研究 PostgreSQL 当前仍受支持的主版本。",
    ],
)
def test_semantic_research_requests_route_without_model_boundary(message: str) -> None:
    decision = AutomaticModeRouter("search-v12").route(message)

    assert decision.mode is SearchMode.RESEARCH


@pytest.mark.parametrize(
    "message",
    [
        "What are the three lowercase JSON literals?",
        "JSON 标准规定的三个小写字面量是什么？请原样列出并引用标准来源。",
    ],
)
def test_single_source_enumeration_can_remain_fast(message: str) -> None:
    decision = AutomaticModeRouter("search-v12").route(message)

    assert decision.mode is SearchMode.FAST


def test_decimal_protocol_version_is_not_treated_as_an_enumeration() -> None:
    decision = AutomaticModeRouter("search-v12").route(
        "Which RFC specifies TLS 1.3? Include the protocol version and RFC number."
    )

    assert decision.mode is SearchMode.FAST


def test_universal_cross_context_claim_routes_to_research() -> None:
    decision = AutomaticModeRouter("search-v12").route(
        "Prove one universally best Apex Legends team for every rank, map, "
        "region, and player skill level."
    )

    assert decision.mode is SearchMode.RESEARCH
    assert "cross_context_universal_claim" in decision.reason_codes


def test_private_multi_attribute_request_routes_to_research() -> None:
    decision = AutomaticModeRouter("search-v12").route(
        "综合公开网页与 Sana 私人记忆，列出过去每局 Apex Legends 的队友、"
        "隐藏分和精确时间。"
    )

    assert decision.mode is SearchMode.RESEARCH
    assert "private_multi_attribute_request" in decision.reason_codes


class CountingBoundaryClassifier:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, message: str) -> RoutingDecision:
        self.calls += 1
        return RoutingDecision(SearchMode.RESEARCH, ("model_boundary",), "search-v12", 0.75)


async def test_boundary_classifier_is_called_at_most_once_for_ambiguous_text() -> None:
    classifier = CountingBoundaryClassifier()
    decision = await AutomaticModeRouter("search-v12").route_with_boundary_classifier(
        "它现在怎么样？",
        classifier,
    )

    assert decision.mode is SearchMode.RESEARCH
    assert classifier.calls == 1
