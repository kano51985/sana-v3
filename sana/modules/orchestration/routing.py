"""Cheap deterministic routing used before deeper planning."""

from __future__ import annotations

import re

from sana.modules.orchestration.domain import RoutingDecision, SearchMode


class AutomaticModeRouter:
    """Rule-first router; a later model classifier may handle only edge cases."""

    _RESEARCH_PATTERNS = (
        re.compile(r"\b(compare|comparison|versus|vs\.?|research|report)\b", re.I),
        re.compile(r"(比较|对比|调研|研究报告|完整来源|逐项核实)"),
    )
    _HIGH_CONSEQUENCE_PATTERNS = (
        re.compile(r"\b(medical|diagnosis|legal|lawsuit|investment|tax)\b", re.I),
        re.compile(r"(医疗|诊断|法律|诉讼|投资|税务)"),
    )

    def __init__(self, policy_version: str) -> None:
        self.policy_version = policy_version

    def route(self, message: str) -> RoutingDecision:
        normalized = " ".join(message.split())
        reasons: list[str] = []
        if any(pattern.search(normalized) for pattern in self._RESEARCH_PATTERNS):
            reasons.append("comparison_or_research_request")
        if any(pattern.search(normalized) for pattern in self._HIGH_CONSEQUENCE_PATTERNS):
            reasons.append("high_consequence")
        question_count = len(re.findall(r"[?？]", normalized))
        if question_count >= 3:
            reasons.append("multiple_required_facts")

        if reasons:
            return RoutingDecision(
                SearchMode.RESEARCH,
                tuple(reasons),
                self.policy_version,
                0.9,
            )
        return RoutingDecision(
            SearchMode.FAST,
            ("single_or_low_complexity_fact",),
            self.policy_version,
            0.8,
        )
