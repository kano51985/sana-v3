"""One primary model call that emits normalized intent and fact requirements."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Protocol

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelInvocationContext,
    ModelMessage,
    ModelResult,
    ModelRole,
)
from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
)
from sana.modules.search_planning.policy import SearchPlanningPolicy
from sana.modules.search_planning.router import AutomaticModeRouter


_COUNT_WORDS = {
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}
_EXPLICIT_COUNT = re.compile(
    r"(?:列出|列举|说明|解释|比较|对比).{0,16}?"
    r"(?P<zh>[二两三四五六七八2-8])(?:种|个|项|类|条)?|"
    r"(?:list|name|explain|compare).{0,16}?"
    r"(?P<en>two|three|four|five|six|seven|eight|[2-8])"
    r"(?:\s+(?:types?|items?|facts?|objects?|options?))?",
    re.I,
)
_EXPLICIT_CROSS_CHECK = re.compile(
    r"(交叉核实|交叉验证|多源核实|cross[- ]?check|verify across|multiple sources?)",
    re.I,
)


def minimum_fact_count(user_message: str, policy_version: str) -> int:
    """Return the deterministic semantic floor that planner output must retain."""

    policy = SearchPlanningPolicy(version=policy_version)
    router_count = len(AutomaticModeRouter(policy_version).infer_fact_types(user_message))
    explicit_counts: list[int] = []
    for match in _EXPLICIT_COUNT.finditer(user_message):
        raw = (match.group("zh") or match.group("en") or "").casefold()
        explicit_counts.append(int(raw) if raw.isdecimal() else _COUNT_WORDS[raw])
    cross_check_count = 2 if _EXPLICIT_CROSS_CHECK.search(user_message) else 1
    return min(
        policy.max_facts,
        max(1, router_count, cross_check_count, *explicit_counts),
    )


class PlanningModelGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


class IntentParser:
    def __init__(
        self,
        policy: SearchPlanningPolicy,
        *,
        allow_optional_facts: bool = False,
        minimum_facts: int = 1,
    ) -> None:
        if not 1 <= minimum_facts <= policy.max_facts:
            raise ValueError("minimum_facts must fit within the planning policy")
        self._policy = policy
        self._allow_optional_facts = allow_optional_facts
        self._minimum_facts = minimum_facts

    def parse(self, text: str) -> NormalizedIntent:
        payload: dict[str, Any] = json.loads(text)
        raw_facts = payload["facts"]
        if (
            not isinstance(raw_facts, list)
            or not self._minimum_facts <= len(raw_facts) <= self._policy.max_facts
        ):
            raise ValueError(
                "facts must satisfy the deterministic minimum and policy maximum"
            )
        facts = tuple(
            FactRequirement(
                key=str(raw["key"]),
                fact_type=FactType(str(raw["fact_type"]).strip().lower()),
                description=str(raw["description"]),
                subject=str(raw.get("subject") or payload["entity"]),
                required=(
                    bool(raw.get("required", True))
                    if self._allow_optional_facts
                    else True
                ),
                freshness=Freshness(
                    str(raw.get("freshness", "STABLE")).strip().upper()
                ),
                consequence=Consequence(
                    str(raw.get("consequence", "LOW")).strip().upper()
                ),
                preferred_source_kinds=tuple(
                    str(item) for item in raw.get("preferred_source_kinds", [])
                ),
            )
            for raw in raw_facts
        )
        return NormalizedIntent(
            entity=str(payload["entity"]),
            aliases=tuple(str(alias) for alias in payload.get("aliases", [])),
            locale=str(payload.get("locale", "en")),
            facts=facts,
            requires_comparison=bool(payload.get("requires_comparison", False)),
            requires_complete_sources=bool(
                payload.get("requires_complete_sources", False)
            ),
        )

    def repair_instruction(self, error: Exception) -> str:
        return (
            "Return only one valid JSON object with entity, aliases, locale, "
            "requires_comparison, requires_complete_sources, and facts. Each fact "
            "must contain key, fact_type, description, subject, required, freshness, "
            "consequence, and preferred_source_kinds. fact_type must be one of "
            "character_changes, version, patch_notes, team_meta, current_value, "
            "comparison, background. freshness must be exactly STABLE, RECENT, or "
            "CURRENT. consequence must be exactly LOW, MEDIUM, or HIGH. Do not add "
            f"markdown or commentary. Return at least {self._minimum_facts} distinct "
            f"facts. Validation error type: {type(error).__name__}."
        )


class SearchPlanner:
    _OPTIONAL_REQUEST = re.compile(
        r"(可选|非必需|如有|如果能找到|optional|if available|if possible)",
        re.I,
    )

    def __init__(
        self,
        gateway: PlanningModelGateway,
        policy: SearchPlanningPolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._policy = policy or SearchPlanningPolicy()

    async def plan(
        self,
        user_message: str,
        *,
        allowed_conversation_summary: str = "",
        deadline: datetime,
        model_budget: ModelCallBudget,
        invocation_context: ModelInvocationContext | None = None,
    ) -> NormalizedIntent:
        minimum_facts = minimum_fact_count(user_message, self._policy.version)
        system = (
            "Normalize the current request into a canonical entity and atomic fact "
            "requirements. Do not turn conversational filler into search terms. "
            "Never collapse separately requested subquestions into one background "
            "fact. When the request names N items and asks about each, emit at least "
            "N distinct facts; use one fact per named or enumerated item. Cross-check "
            "requests need separate facts for the source perspectives being checked. "
            "Mark every requested fact required=true unless the user explicitly says "
            "that part is optional. "
            "Return one JSON object only with entity, aliases, locale, "
            "requires_comparison, requires_complete_sources, and facts. Each fact "
            "must contain key, fact_type, description, subject, required, freshness, "
            "consequence, and preferred_source_kinds. Allowed fact_type values are "
            "character_changes, version, patch_notes, team_meta, current_value, "
            "comparison, background. Use uppercase STABLE, RECENT, or CURRENT for "
            "freshness and uppercase LOW, MEDIUM, or HIGH for consequence. "
            f"This request requires at least {minimum_facts} distinct facts."
        )
        user = f"Current request:\n{user_message.strip()}"
        if allowed_conversation_summary.strip():
            user += (
                "\nAllowed context summary (resolve references only; never copy it as "
                f"a query suffix):\n{allowed_conversation_summary.strip()}"
            )
        parser = IntentParser(
            self._policy,
            allow_optional_facts=bool(self._OPTIONAL_REQUEST.search(user_message)),
            minimum_facts=minimum_facts,
        )
        result = await self._gateway.generate(
            ModelRole.PLANNER,
            (
                ModelMessage(MessageRole.SYSTEM, system),
                ModelMessage(MessageRole.USER, user),
            ),
            deadline=deadline,
            budget=model_budget,
            parser=parser,
            invocation_context=invocation_context,
        )
        if not isinstance(result.parsed, NormalizedIntent):
            raise TypeError("Planner gateway did not return a NormalizedIntent")
        return result.parsed
