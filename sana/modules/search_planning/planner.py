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


class PlanningModelGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


class IntentParser:
    def __init__(
        self,
        policy: SearchPlanningPolicy,
        *,
        allow_optional_facts: bool = False,
    ) -> None:
        self._policy = policy
        self._allow_optional_facts = allow_optional_facts

    def parse(self, text: str) -> NormalizedIntent:
        payload: dict[str, Any] = json.loads(text)
        raw_facts = payload["facts"]
        if not isinstance(raw_facts, list) or not 1 <= len(raw_facts) <= self._policy.max_facts:
            raise ValueError("facts must be a non-empty bounded list")
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
            f"markdown or commentary. Validation error type: {type(error).__name__}."
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
            "freshness and uppercase LOW, MEDIUM, or HIGH for consequence."
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
