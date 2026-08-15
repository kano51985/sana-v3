import json
from pathlib import Path

from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.router import AutomaticModeRouter
from sana.modules.orchestration.domain import RoutingDecision


FIXTURE = Path(__file__).parents[2] / "fixtures" / "evals" / "apex_multi_fact.json"


def test_apex_multi_fact_request_routes_directly_to_research() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))

    decision = AutomaticModeRouter("search-v3").route(case["user_message"])

    assert decision.mode is SearchMode.RESEARCH
    assert "three_or_more_required_facts" in decision.reason_codes
    assert "fresh_multi_fact" in decision.reason_codes
    assert decision.policy_version == "search-v3"


def test_simple_single_fact_stays_fast() -> None:
    decision = AutomaticModeRouter("search-v3").route("Apex Legends 是哪一年发布的？")
    assert decision.mode is SearchMode.FAST


def test_high_consequence_request_routes_research_for_cross_check() -> None:
    decision = AutomaticModeRouter("search-v3").route("这个医疗诊断结论可信吗？")
    assert decision.mode is SearchMode.RESEARCH
    assert "high_consequence_cross_check" in decision.reason_codes


def test_enumerated_and_cross_check_requests_route_to_research() -> None:
    router = AutomaticModeRouter("search-v3")

    enumerated = router.route("列出四种 Git 对象类型，并分别说明每种对象的用途。")
    cross_check = router.route(
        "Find and cross-check private model weights using multiple sources."
    )

    assert enumerated.mode is SearchMode.RESEARCH
    assert "enumerated_multi_fact" in enumerated.reason_codes
    assert cross_check.mode is SearchMode.RESEARCH
    assert "explicit_cross_check" in cross_check.reason_codes


class CountingBoundaryClassifier:
    def __init__(self) -> None:
        self.calls = 0

    async def classify(self, message: str) -> RoutingDecision:
        self.calls += 1
        return RoutingDecision(SearchMode.RESEARCH, ("model_boundary",), "search-v3", 0.75)


async def test_boundary_classifier_is_called_at_most_once_for_ambiguous_text() -> None:
    classifier = CountingBoundaryClassifier()
    decision = await AutomaticModeRouter("search-v3").route_with_boundary_classifier(
        "它现在怎么样？",
        classifier,
    )

    assert decision.mode is SearchMode.RESEARCH
    assert classifier.calls == 1
