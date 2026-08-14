"""Create bounded, deduplicated query revisions for valuable fact gaps."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from sana.modules.evidence.evidence_gain import ExpectedEvidenceGain
from sana.modules.orchestration.domain import SearchMode, StepType
from sana.modules.search_planning.domain import NormalizedIntent, QuerySpec
from sana.modules.search_planning.query_compiler import QueryCompiler


class ExpansionStopReason(StrEnum):
    EXPAND = "EXPAND"
    MAX_ROUNDS = "MAX_ROUNDS"
    LOW_EXPECTED_GAIN = "LOW_EXPECTED_GAIN"
    QUERY_BUDGET_EXHAUSTED = "QUERY_BUDGET_EXHAUSTED"
    NO_REQUIRED_GAPS = "NO_REQUIRED_GAPS"


@dataclass(frozen=True, slots=True)
class RevisionStep:
    step_key: str
    step_type: StepType
    plan_revision: int
    query_key: str


@dataclass(frozen=True, slots=True)
class ExpansionDecision:
    should_expand: bool
    plan_revision: int
    queries: tuple[QuerySpec, ...]
    steps: tuple[RevisionStep, ...]
    expected_gain: float
    reason: ExpansionStopReason
    gain_by_fact: Mapping[str, float]

    def __post_init__(self) -> None:
        if self.plan_revision < 1:
            raise ValueError("Expansion revision must be positive")
        if not 0 <= self.expected_gain <= 1:
            raise ValueError("Expansion expected gain must be between zero and one")
        if self.should_expand != bool(self.queries and self.steps):
            raise ValueError("Expansion decision does not match generated work")
        if self.should_expand and self.reason is not ExpansionStopReason.EXPAND:
            raise ValueError("Expanding decision requires EXPAND reason")
        if not self.should_expand and self.reason is ExpansionStopReason.EXPAND:
            raise ValueError("Stopped decision cannot use EXPAND reason")
        if len(self.queries) != len(self.steps):
            raise ValueError("Every expansion query requires one discovery Step")
        if any(query.plan_revision != self.plan_revision for query in self.queries):
            raise ValueError("Expansion query belongs to another revision")
        if any(
            step.plan_revision != self.plan_revision
            or step.query_key != query.key
            for step, query in zip(self.steps, self.queries)
        ):
            raise ValueError("Expansion Step does not match its query")
        if any(not 0 <= value <= 1 for value in self.gain_by_fact.values()):
            raise ValueError("Per-fact evidence gain must be between zero and one")
        object.__setattr__(self, "gain_by_fact", MappingProxyType(dict(self.gain_by_fact)))


class ExpansionPlanner:
    def __init__(
        self,
        compiler: QueryCompiler | None = None,
        *,
        minimum_expected_gain: float = 0.4,
        max_expansion_rounds: int = 2,
    ) -> None:
        if not 0 <= minimum_expected_gain <= 1 or max_expansion_rounds < 1:
            raise ValueError("Expansion policy values are invalid")
        self._compiler = compiler or QueryCompiler()
        self._minimum_gain = minimum_expected_gain
        self._max_rounds = max_expansion_rounds

    def plan(
        self,
        intent: NormalizedIntent,
        *,
        current_revision: int,
        completed_expansion_rounds: int,
        gap_fact_keys: frozenset[str],
        gains: tuple[ExpectedEvidenceGain, ...],
        existing_queries: tuple[QuerySpec, ...],
    ) -> ExpansionDecision:
        if current_revision < 1 or completed_expansion_rounds < 0:
            raise ValueError("Expansion revision and round counters are invalid")
        next_revision = current_revision + 1
        if completed_expansion_rounds >= self._max_rounds:
            return self._stop(next_revision, ExpansionStopReason.MAX_ROUNDS)
        if not gap_fact_keys:
            return self._stop(next_revision, ExpansionStopReason.NO_REQUIRED_GAPS)
        gain_by_fact = {
            gain.fact_key: gain.score
            for gain in gains
            if gain.fact_key in gap_fact_keys
        }
        selected_keys = frozenset(
            key
            for key, score in gain_by_fact.items()
            if score >= self._minimum_gain
        )
        if not selected_keys:
            return self._stop(
                next_revision,
                ExpansionStopReason.LOW_EXPECTED_GAIN,
                gain_by_fact=gain_by_fact,
            )
        selected_facts = tuple(
            fact
            for fact in intent.facts
            if fact.required and fact.key in selected_keys
        )
        if not selected_facts:
            return self._stop(
                next_revision,
                ExpansionStopReason.NO_REQUIRED_GAPS,
                gain_by_fact=gain_by_fact,
            )
        selected_keys = frozenset(fact.key for fact in selected_facts)
        narrowed_intent = NormalizedIntent(
            entity=intent.entity,
            aliases=intent.aliases,
            locale=intent.locale,
            facts=selected_facts,
            requires_comparison=intent.requires_comparison,
            requires_complete_sources=intent.requires_complete_sources,
        )
        existing_signatures = frozenset(query.signature for query in existing_queries)
        queries = self._compiler.compile(
            narrowed_intent,
            SearchMode.RESEARCH,
            plan_revision=next_revision,
            expansion=True,
            existing_signatures=existing_signatures,
        )
        if not queries:
            return self._stop(
                next_revision,
                ExpansionStopReason.QUERY_BUDGET_EXHAUSTED,
                gain_by_fact=gain_by_fact,
            )
        steps = tuple(
            RevisionStep(
                step_key=f"discover:{query.key}",
                step_type=StepType.DISCOVERY,
                plan_revision=next_revision,
                query_key=query.key,
            )
            for query in queries
        )
        expected_gain = sum(gain_by_fact[key] for key in selected_keys) / len(
            selected_keys
        )
        return ExpansionDecision(
            True,
            next_revision,
            queries,
            steps,
            expected_gain,
            ExpansionStopReason.EXPAND,
            gain_by_fact,
        )

    @staticmethod
    def _stop(
        next_revision: int,
        reason: ExpansionStopReason,
        *,
        gain_by_fact: Mapping[str, float] = MappingProxyType({}),
    ) -> ExpansionDecision:
        return ExpansionDecision(
            False,
            next_revision,
            (),
            (),
            0.0,
            reason,
            gain_by_fact,
        )
