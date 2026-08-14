import asyncio
import json
from pathlib import Path

from sana.modules.orchestration.evaluation import evaluate_cases, load_cases
from sana.modules.orchestration.shadow import ShadowOutcome, ShadowRunner


FIXTURES = Path(__file__).parents[2] / "evals" / "search_cases.jsonl"


def test_search_acceptance_fixture_suite_passes_all_quality_gates() -> None:
    report = evaluate_cases(load_cases(FIXTURES))

    assert report.passed is True
    assert report.mode_accuracy == 1.0
    assert report.effective_mode_accuracy == 1.0
    assert report.query_pollution_rate == 0.0
    assert report.explicit_gap_rate == 1.0
    assert report.citation_traceability == 1.0
    assert report.deadline_compliance == 1.0


def test_apex_regression_routes_research_without_conversation_pollution() -> None:
    report = evaluate_cases(load_cases(FIXTURES))
    apex = next(item for item in report.cases if item.case_id == "apex-regression")

    assert apex.actual_initial_mode == "RESEARCH"
    assert apex.actual_effective_mode == "RESEARCH"
    assert apex.query_pollution_count == 0
    assert apex.required_fact_count == 4
    assert apex.covered_fact_count + apex.explicit_gap_count == 4
    assert apex.citation_traceability == 1.0


def test_eval_report_contains_metrics_not_raw_messages_or_queries() -> None:
    report = evaluate_cases(load_cases(FIXTURES))
    rendered = json.dumps(report.to_dict(), ensure_ascii=False)

    assert "我好久没碰apex" not in rendered
    assert "你不是一直在玩吗" not in rendered
    assert "可以告诉我" not in rendered
    assert "Apex Legends 当前版本" not in rendered


class MemorySink:
    def __init__(self) -> None:
        self.payloads = []

    async def write(self, payload) -> None:
        self.payloads.append(payload)


async def test_shadow_pipeline_cannot_delay_or_replace_primary_visible_result() -> None:
    sink = MemorySink()
    runner = ShadowRunner(sink)
    release_shadow = asyncio.Event()
    primary_result = object()

    async def primary():
        return primary_result, ShadowOutcome("FAST", "PARTIAL", 100, 0.01, 1, 2, 1, 0)

    async def shadow():
        await release_shadow.wait()
        return ShadowOutcome("RESEARCH", "COMPLETE", 200, 0.02, 2, 2, 1, 0)

    visible = await asyncio.wait_for(runner.execute(primary, shadow), timeout=0.2)

    assert visible is primary_result
    assert sink.payloads == []
    release_shadow.set()
    await runner.drain()
    assert len(sink.payloads) == 1
    assert sink.payloads[0]["delta"]["covered"] == 1


async def test_shadow_failure_is_isolated_and_error_message_is_not_saved() -> None:
    sink = MemorySink()
    runner = ShadowRunner(sink)

    async def primary():
        return "visible", ShadowOutcome("FAST", "COMPLETE", 100, 0.01, 1, 1, 1, 0)

    async def shadow():
        raise RuntimeError("private prompt and body")

    assert await runner.execute(primary, shadow) == "visible"
    await runner.drain()

    rendered = repr(sink.payloads)
    assert "private prompt" not in rendered
    assert sink.payloads[0]["error_type"] == "RuntimeError"
    assert sink.payloads[0]["candidate"] is None
