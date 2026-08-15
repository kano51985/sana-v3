"""Deterministic offline acceptance evaluation for search architecture fixtures."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sana.modules.evidence.coverage import CoverageAssessment, FactCoverage
from sana.modules.evidence.domain import EvidenceLevel
from sana.modules.orchestration.domain import SearchMode
from sana.modules.orchestration.research_workflow import FastUpgradePolicy
from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
)
from sana.modules.search_planning.query_compiler import QueryCompiler
from sana.modules.search_planning.router import AutomaticModeRouter


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    case_id: str
    expected_initial_mode: str
    actual_initial_mode: str
    expected_effective_mode: str
    actual_effective_mode: str
    query_count: int
    query_pollution_count: int
    required_fact_count: int
    covered_fact_count: int
    explicit_gap_count: int
    citation_traceability: float
    latency_ms: int
    cost_usd: float
    within_deadline: bool
    passed: bool


@dataclass(frozen=True, slots=True)
class EvalReport:
    case_count: int
    passed_count: int
    mode_accuracy: float
    effective_mode_accuracy: float
    query_pollution_rate: float
    fact_coverage_rate: float
    explicit_gap_rate: float
    citation_traceability: float
    deadline_compliance: float
    average_latency_ms: float
    average_cost_usd: float
    cases: tuple[EvalCaseResult, ...]

    @property
    def passed(self) -> bool:
        return self.case_count > 0 and self.case_count == self.passed_count

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return payload


def load_cases(path: str | Path) -> tuple[dict[str, Any], ...]:
    cases = []
    for line_number, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Eval fixture line {line_number} must be an object")
        cases.append(value)
    if not cases:
        raise ValueError("Eval fixture file is empty")
    return tuple(cases)


def _stable_id(case_id: str, value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"sana-eval:{case_id}:{value}")


def _intent(case: dict[str, Any]) -> NormalizedIntent:
    facts = tuple(
        FactRequirement(
            key=str(item["key"]),
            fact_type=FactType(str(item["fact_type"])),
            description=str(item["description"]),
            subject=str(item["subject"]),
            required=bool(item.get("required", True)),
            freshness=Freshness(str(item.get("freshness", "STABLE"))),
            consequence=Consequence(str(item.get("consequence", "LOW"))),
            preferred_source_kinds=tuple(item.get("preferred_source_kinds", ())),
        )
        for item in case["facts"]
    )
    return NormalizedIntent(
        entity=str(case["entity"]),
        aliases=tuple(case.get("aliases", ())),
        locale=str(case["locale"]),
        facts=facts,
        requires_comparison=bool(case.get("requires_comparison", False)),
        requires_complete_sources=bool(case.get("requires_complete_sources", False)),
    )


def evaluate_case(case: dict[str, Any]) -> EvalCaseResult:
    case_id = str(case["id"])
    intent = _intent(case)
    router = AutomaticModeRouter("search-v7")
    routing = router.route(str(case["user_message"]))
    queries = QueryCompiler().compile(intent, routing.mode)
    forbidden = tuple(str(item).casefold() for item in case.get("forbidden_query_terms", ()))
    pollution_count = sum(
        any(term and term in query.text.casefold() for term in forbidden)
        for query in queries
    )

    tenant_id = _stable_id(case_id, "tenant")
    run_id = _stable_id(case_id, "run")
    fact_ids = {fact.key: _stable_id(case_id, fact.key) for fact in intent.facts}
    outcomes = dict(case["fact_outcomes"])
    coverage: dict[UUID, CoverageAssessment] = {}
    covered_count = 0
    explicit_gap_count = 0
    required_count = 0
    for fact in intent.facts:
        if not fact.required:
            continue
        required_count += 1
        outcome = dict(outcomes[fact.key])
        status = FactCoverage(str(outcome["status"]))
        if status in {FactCoverage.COVERED, FactCoverage.VERIFIED}:
            covered_count += 1
        if status in {FactCoverage.OPEN, FactCoverage.PARTIAL} and str(
            outcome.get("gap_reason", "")
        ).strip():
            explicit_gap_count += 1
        fact_id = fact_ids[fact.key]
        coverage[fact_id] = CoverageAssessment(
            tenant_id,
            run_id,
            fact_id,
            fact.key,
            status,
            (
                EvidenceLevel.L2_VERIFIED
                if status is FactCoverage.VERIFIED
                else EvidenceLevel.L1_GROUNDED
                if status in {FactCoverage.COVERED, FactCoverage.PARTIAL}
                else None
            ),
            (),
            (),
            (),
            (),
            0,
            status is FactCoverage.PARTIAL,
        )

    upgrade = (
        FastUpgradePolicy().evaluate(
            tenant_id=tenant_id,
            run_id=run_id,
            intent=intent,
            fact_ids=fact_ids,
            coverage=coverage,
        )
        if routing.mode is SearchMode.FAST
        else None
    )
    effective_mode = (
        SearchMode.RESEARCH
        if routing.mode is SearchMode.RESEARCH or (upgrade and upgrade.should_upgrade)
        else SearchMode.FAST
    )
    claims = tuple(case.get("claims", ()))
    factual_claims = tuple(claim for claim in claims if claim.get("kind") == "FACTUAL")
    traceable = sum(
        bool(claim.get("evidence_ids"))
        and set(claim.get("evidence_ids", ()))
        <= set(claim.get("citation_evidence_ids", ()))
        for claim in factual_claims
    )
    citation_rate = traceable / len(factual_claims) if factual_claims else 1.0
    latency_ms = int(case["latency_ms"])
    expected_initial = str(case["expected_initial_mode"])
    expected_effective = str(case["expected_effective_mode"])
    deadline_ms = 15_000 if effective_mode is SearchMode.FAST else 120_000
    within_deadline = 0 <= latency_ms <= deadline_ms
    gaps_expected = required_count - covered_count
    gaps_explicit = explicit_gap_count == gaps_expected
    passed = all(
        (
            routing.mode.value == expected_initial,
            effective_mode.value == expected_effective,
            pollution_count == 0,
            gaps_explicit,
            citation_rate == 1.0,
            within_deadline,
        )
    )
    return EvalCaseResult(
        case_id,
        expected_initial,
        routing.mode.value,
        expected_effective,
        effective_mode.value,
        len(queries),
        pollution_count,
        required_count,
        covered_count,
        explicit_gap_count,
        citation_rate,
        latency_ms,
        float(case["cost_usd"]),
        within_deadline,
        passed,
    )


def evaluate_cases(cases: tuple[dict[str, Any], ...]) -> EvalReport:
    results = tuple(evaluate_case(case) for case in cases)
    count = len(results)
    query_count = sum(result.query_count for result in results)
    required = sum(result.required_fact_count for result in results)
    uncovered = sum(
        result.required_fact_count - result.covered_fact_count for result in results
    )
    factual_case_rates = [result.citation_traceability for result in results]
    return EvalReport(
        case_count=count,
        passed_count=sum(result.passed for result in results),
        mode_accuracy=sum(
            result.actual_initial_mode == result.expected_initial_mode for result in results
        )
        / count,
        effective_mode_accuracy=sum(
            result.actual_effective_mode == result.expected_effective_mode
            for result in results
        )
        / count,
        query_pollution_rate=(
            sum(result.query_pollution_count for result in results) / query_count
            if query_count
            else 0.0
        ),
        fact_coverage_rate=(
            sum(result.covered_fact_count for result in results) / required
            if required
            else 1.0
        ),
        explicit_gap_rate=(
            sum(result.explicit_gap_count for result in results) / uncovered
            if uncovered
            else 1.0
        ),
        citation_traceability=sum(factual_case_rates) / count,
        deadline_compliance=sum(result.within_deadline for result in results) / count,
        average_latency_ms=sum(result.latency_ms for result in results) / count,
        average_cost_usd=sum(result.cost_usd for result in results) / count,
        cases=results,
    )
