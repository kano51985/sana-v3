"""Rule-first FAST/RESEARCH selection with auditable reason codes."""

from __future__ import annotations

import re
from typing import Protocol

from sana.modules.orchestration.domain import RoutingDecision, SearchMode
from sana.modules.search_planning.domain import FactType


class BoundaryClassifier(Protocol):
    async def classify(self, message: str) -> RoutingDecision: ...


class AutomaticModeRouter:
    _FACT_PATTERNS = {
        FactType.CHARACTER_CHANGES: re.compile(r"(改动|调整|削弱|加强|changes?|buff|nerf)", re.I),
        FactType.VERSION: re.compile(r"(当前版本|版本号|current version|season)", re.I),
        FactType.PATCH_NOTES: re.compile(r"(补丁|更新说明|patch notes?|changelog)", re.I),
        FactType.TEAM_META: re.compile(r"(配队|阵容|meta|team comp|lineup)", re.I),
        FactType.COMPARISON: re.compile(r"(比较|对比|versus|\bvs\.?\b|compare)", re.I),
        FactType.CURRENT_VALUE: re.compile(r"(价格|汇率|比分|current price|exchange rate|score)", re.I),
    }
    _HIGH_CONSEQUENCE = re.compile(
        r"(医疗|诊断|法律|诉讼|投资|税务|medical|diagnosis|legal|investment|tax)",
        re.I,
    )
    _COMPLETE_SOURCES = re.compile(
        r"(完整来源|全部来源|逐项核实|研究报告|complete sources?|full report)",
        re.I,
    )
    _CROSS_CHECK = re.compile(
        r"(交叉核实|交叉验证|多源核实|cross[- ]?check|verify across|multiple sources?)",
        re.I,
    )
    _ENUMERATED_MULTI_FACT = re.compile(
        r"(列出.{0,24}(?:三|四|五|六|七|八|九|(?<![.\d])[3-9](?![.\d]))(?:种|个|项)?.{0,32}(?:分别|逐一)|"
        r"(?:三|四|五|六|七|八|九|(?<![.\d])[3-9](?![.\d]))(?:种|个|项|类|条)"
        r"(?:性质|状态|类型|术语|协议)|"
        r"(?:three|four|five|six|seven|eight|nine|(?<![.\d])[3-9](?![.\d])).{0,48}"
        r"(?:properties|states?|protocols?|levels?)|"
        r"(?:three|four|five|six|seven|eight|nine|(?<![.\d])[3-9](?![.\d])).{0,32}(?:types?|items?|facts?).{0,32}(?:each|explain))",
        re.I,
    )
    _CROSS_CONTEXT_UNIVERSAL = re.compile(
        r"(?:universally|universal|every|all|所有|全部).{0,160}"
        r"(?:rank|map|region|skill|段位|地图|地区|水平)",
        re.I,
    )
    _PRIVATE_MULTI_ATTRIBUTE = re.compile(
        r"(?:private|memory|私人记忆|私有|隐藏分).{0,160}"
        r"(?:teammates?|hidden\s+(?:rating|mmr)|exact\s+time|队友|隐藏分|精确时间)"
        r".{0,100}(?:,|、|\band\b|和).{0,100}"
        r"(?:teammates?|hidden\s+(?:rating|mmr)|exact\s+time|队友|隐藏分|精确时间)",
        re.I,
    )
    _EXPLICIT_RESEARCH = re.compile(r"(?:研究|调研|\bresearch\b)", re.I)
    _FRESHNESS = re.compile(r"(最近|最新|当前|今天|recent|latest|current|today)", re.I)

    def __init__(self, policy_version: str) -> None:
        self.policy_version = policy_version

    def infer_fact_types(self, message: str) -> frozenset[FactType]:
        return frozenset(
            fact_type
            for fact_type, pattern in self._FACT_PATTERNS.items()
            if pattern.search(message)
        )

    def route(self, message: str) -> RoutingDecision:
        fact_types = self.infer_fact_types(message)
        reasons: list[str] = []
        if len(fact_types) >= 3:
            reasons.append("three_or_more_required_facts")
        if FactType.COMPARISON in fact_types:
            reasons.append("comparison_or_multi_hop")
        if self._FRESHNESS.search(message) and len(fact_types) >= 2:
            reasons.append("fresh_multi_fact")
        if self._HIGH_CONSEQUENCE.search(message):
            reasons.append("high_consequence_cross_check")
        if self._COMPLETE_SOURCES.search(message):
            reasons.append("complete_source_requirement")
        if self._CROSS_CHECK.search(message):
            reasons.append("explicit_cross_check")
        if self._ENUMERATED_MULTI_FACT.search(message):
            reasons.append("enumerated_multi_fact")
        if self._EXPLICIT_RESEARCH.search(message):
            reasons.append("explicit_research_request")
        if self._CROSS_CONTEXT_UNIVERSAL.search(message):
            reasons.append("cross_context_universal_claim")
        if self._PRIVATE_MULTI_ATTRIBUTE.search(message):
            reasons.append("private_multi_attribute_request")

        if reasons:
            return RoutingDecision(
                SearchMode.RESEARCH,
                tuple(dict.fromkeys(reasons)),
                self.policy_version,
                0.95,
            )
        confidence = 0.85 if fact_types else 0.7
        return RoutingDecision(
            SearchMode.FAST,
            ("single_or_low_complexity_fact",),
            self.policy_version,
            confidence,
        )

    async def route_with_boundary_classifier(
        self,
        message: str,
        classifier: BoundaryClassifier | None,
    ) -> RoutingDecision:
        decision = self.route(message)
        if decision.confidence >= 0.8 or classifier is None:
            return decision
        classified = await classifier.classify(message)
        if classified.policy_version != self.policy_version:
            raise ValueError("Boundary classifier returned a different policy version")
        return classified
