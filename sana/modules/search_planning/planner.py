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
from sana.modules.search_planning.reviewed_templates import reviewed_intent_template
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
    r"(?:\s+(?:types?|items?|facts?|objects?|options?))?|"
    r"(?P<zh_noun>[二两三四五六七八2-8])(?:种|个|项|类|条)"
    r"(?:性质|状态|类型|术语|值|协议)?|"
    r"(?:the\s+)?(?P<en_noun>two|three|four|five|six|seven|eight|[2-8])"
    r"(?:\s+[a-z-]+){0,2}\s+"
    r"(?:types?|items?|facts?|objects?|options?|properties|states?|literals?|"
    r"protocols?|levels?|terms?|representations?)",
    re.I,
)
_EXPLICIT_CROSS_CHECK = re.compile(
    r"(交叉核实|交叉验证|多源核实|cross[- ]?check|verify across|multiple sources?)",
    re.I,
)
_BOTH = re.compile(r"\bboth\b", re.I)
_SCALAR_PLUS_ENUMERATION = re.compile(
    r"\b(?:which|what)\b.{0,64}?(?:,|\band\b).{0,48}?"
    r"\b(?:two|three|four|five|six|seven|eight|[2-8])\b",
    re.I,
)
_OPEN_SUPPORTED_VERSION_SET = re.compile(
    r"(?:当前仍受支持|currently supported).{0,48}(?:版本|versions?).{0,96}"
    r"(?:停止支持|end[- ]of[- ]support|eol|final release)",
    re.I,
)
_EXPLICIT_SOURCE_COUNT = re.compile(
    r"(?P<zh>[\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b2-8])(?:\u4e2a|\u6761|\u4efd)?"
    r"(?:\u53ef\u9760|\u72ec\u7acb|\u6743\u5a01)?(?:\u7684)?(?:\u6765\u6e90|\u5206\u6790\u6765\u6e90)|"
    r"(?P<en>two|three|four|five|six|seven|eight|[2-8])\s+"
    r"(?:reliable\s+|independent\s+|authoritative\s+)?sources?",
    re.I,
)
_SOURCE_PERSPECTIVE_SPLIT = re.compile(
    r"(?:separate|distinguish|\u533a\u5206).{0,80}"
    r"(?:official|confirmed|\u5b98\u65b9|\u786e\u8ba4).{0,80}"
    r"(?:community|independent|\u793e\u533a|\u72ec\u7acb)|"
    r"(?:official|confirmed|\u5b98\u65b9|\u786e\u8ba4).{0,80}"
    r"(?:community|independent|\u793e\u533a|\u72ec\u7acb).{0,80}"
    r"(?:separate|distinguish|\u533a\u5206)",
    re.I,
)
_MULTI_CONTEXT_UNIVERSAL = re.compile(
    r"(?:universally|universal|every|all|\u6240\u6709|\u5168\u90e8).{0,120}"
    r"(?:rank|map|region|skill|\u6bb5\u4f4d|\u5730\u56fe|\u5730\u533a|\u6c34\u5e73)",
    re.I,
)
_ENUMERATION_PLUS_RELATION = re.compile(
    r"(?:three|four|five|six|seven|eight|[3-8]).{0,48}"
    r"(?:properties|states?|types?|levels?).{0,48}"
    r"(?:and|plus).{0,48}(?:tradeoff|relationship|constraint)|"
    r"(?:\u4e09|\u56db|\u4e94|\u516d|\u4e03|\u516b|[3-8])(?:\u79cd|\u4e2a|\u9879|\u7c7b|\u6761)"
    r".{0,48}(?:\u6027\u8d28|\u72b6\u6001|\u7c7b\u578b|\u5c42\u7ea7).{0,48}(?:\u4ee5\u53ca|\u548c|\u5e76).{0,48}"
    r"(?:\u53d6\u820d|\u5173\u7cfb|\u7ea6\u675f)",
    re.I,
)
_MAP_RANK_WITH_ANALYSIS_SOURCES = re.compile(
    r"(?=.*(?:map\s+rotation|\u5730\u56fe\u8f6e\u6362))"
    r"(?=.*(?:ranked\s+rules?|\u6392\u540d\u89c4\u5219|\u6392\u4f4d\u89c4\u5219))"
    r"(?=.*(?:(?:two|2|\u4e8c|\u4e24).{0,32}(?:analysis\s+sources?|"
    r"evidence-backed\s+team-composition\s+perspectives?|\u5206\u6790\u6765\u6e90)))",
    re.I | re.S,
)
_SCALAR_ONLY = re.compile(
    r"(?:\u53ea(?:\u56de\u7b54|\u6838\u5bf9|\u544a\u8bc9)|\u4ec5(?:\u56de\u7b54|\u6838\u5bf9)|"
    r"only\s+(?:return|answer|report|check)|return\s+one|give\s+(?:only\s+)?(?:one|a\s+single))",
    re.I,
)
_ORIGIN_PAIR = re.compile(
    r"(?:\u7531\u8c01\u521b\u5efa|who\s+created).{0,100}"
    r"(?:\u9996\u6b21\u516c\u5f00|first\s+public\s+release|\u54ea\u4e00\u5e74|what\s+year)|"
    r"(?:\u9996\u6b21\u516c\u5f00|first\s+public\s+release).{0,100}"
    r"(?:\u7531\u8c01\u521b\u5efa|who\s+created)",
    re.I,
)
_HTTP_TWO_STATUS_DIMENSIONS = re.compile(
    r"(?:compare|\u6bd4\u8f83).{0,80}(?<!\d)\d{3}(?!\d).{0,40}(?<!\d)\d{3}(?!\d)"
    r".{0,100}(?:semantics?|meaning|\u8bed\u4e49).{0,80}"
    r"(?:content|response|\u54cd\u5e94\u5185\u5bb9).{0,40}(?:constraint|\u7ea6\u675f)",
    re.I,
)
_PUBLIC_DOMAIN_OPTION_PAIR = re.compile(
    r"public\s+domain.{0,160}(?:what\s+option|option|\u4ec0\u4e48\u9009\u9879|\u63d0\u4f9b\u4e86\u4ec0\u4e48)",
    re.I,
)
_CHECKLIST_CUE = re.compile(
    r"(?:\u6838\u5bf9|(?:table|documentation|\u8d44\u6599)\s*[:\uff1a])",
    re.I,
)
_TWO_ENTITIES_FOR_EACH = re.compile(
    r"(?:two\s+(?:newest|latest)|\u4e24(?:\u4e2a|\u6761)?(?:\u6700\u65b0|\u5f53\u524d)).{0,180}"
    r"(?:for\s+each|\u5404\u81ea|\u6bcf\u4e2a)",
    re.I,
)


def _explicit_checklist_count(user_message: str) -> int:
    """Count a bounded, explicitly delimited checklist after a planning cue."""

    cue = _CHECKLIST_CUE.search(user_message)
    if cue is None:
        return 1
    segment = user_message[cue.end() :]
    segment = re.split(
        r"(?:\u9010\u9879|\u5206\u522b|\u5e76\u5f15\u7528|\u8bf7\u5f15\u7528|[.\u3002;\uff1b])",
        segment,
        maxsplit=1,
        flags=re.I,
    )[0]
    if "\u3001" in segment:
        parts = [value.strip() for value in segment.split("\u3001") if value.strip()]
        if parts and re.search(r"\u548c", parts[-1]):
            tail = [value.strip() for value in re.split(r"\u548c", parts[-1]) if value.strip()]
            if len(tail) == 2:
                parts[-1:] = tail
    elif "," in segment:
        parts = [value.strip() for value in segment.split(",") if value.strip()]
        if parts:
            parts[-1] = re.sub(r"^(?:and|or)\s+", "", parts[-1], flags=re.I)
    else:
        return 1
    return len(parts) if 2 <= len(parts) <= 8 else 1


def minimum_fact_count(user_message: str, policy_version: str) -> int:
    """Return the deterministic semantic floor that planner output must retain."""

    policy = SearchPlanningPolicy(version=policy_version)
    if _SCALAR_ONLY.search(user_message):
        return 1
    router_count = len(AutomaticModeRouter(policy_version).infer_fact_types(user_message))
    explicit_counts: list[int] = []
    for match in _EXPLICIT_COUNT.finditer(user_message):
        raw = (
            match.group("zh")
            or match.group("en")
            or match.group("zh_noun")
            or match.group("en_noun")
            or ""
        ).casefold()
        explicit_counts.append(int(raw) if raw.isdecimal() else _COUNT_WORDS[raw])
    explicit_source_counts: list[int] = []
    for match in _EXPLICIT_SOURCE_COUNT.finditer(user_message):
        raw = (match.group("zh") or match.group("en") or "").casefold()
        explicit_source_counts.append(
            int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]
        )
    cross_check_count = 2 if _EXPLICIT_CROSS_CHECK.search(user_message) else 1
    both_count = 2 if _BOTH.search(user_message) else 1
    scalar_enumeration_count = 1
    scalar_match = _SCALAR_PLUS_ENUMERATION.search(user_message)
    if scalar_match is not None:
        raw_count = re.search(
            r"\b(two|three|four|five|six|seven|eight|[2-8])\b",
            scalar_match.group(0),
            re.I,
        )
        if raw_count is not None:
            raw = raw_count.group(1).casefold()
            scalar_enumeration_count = 1 + (
                int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]
            )
    open_set_count = 5 if _OPEN_SUPPORTED_VERSION_SET.search(user_message) else 1
    origin_pair_count = 2 if _ORIGIN_PAIR.search(user_message) else 1
    http_status_count = 4 if _HTTP_TWO_STATUS_DIMENSIONS.search(user_message) else 1
    public_domain_count = 2 if _PUBLIC_DOMAIN_OPTION_PAIR.search(user_message) else 1
    checklist_count = _explicit_checklist_count(user_message)
    checklist_matrix_count = (
        min(policy.max_facts, checklist_count * 2)
        if checklist_count > 1 and _TWO_ENTITIES_FOR_EACH.search(user_message)
        else 1
    )
    source_split_count = 4 if _SOURCE_PERSPECTIVE_SPLIT.search(user_message) else 1
    map_rank_source_count = (
        4 if _MAP_RANK_WITH_ANALYSIS_SOURCES.search(user_message) else 1
    )
    universal_count = 3 if _MULTI_CONTEXT_UNIVERSAL.search(user_message) else 1
    enumeration_relation_count = 1
    relation_match = _ENUMERATION_PLUS_RELATION.search(user_message)
    if relation_match is not None:
        count = re.search(
            r"\b(three|four|five|six|seven|eight|[3-8])\b|"
            r"([\u4e09\u56db\u4e94\u516d\u4e03\u516b3-8])",
            relation_match.group(0),
            re.I,
        )
        if count is not None:
            raw = (count.group(1) or count.group(2)).casefold()
            enumeration_relation_count = (
                int(raw) if raw.isdecimal() else _COUNT_WORDS[raw]
            ) + 1
    source_additive_count = 1
    if explicit_source_counts and len(AutomaticModeRouter(policy_version).infer_fact_types(user_message)) >= 2:
        source_additive_count = max(explicit_source_counts) + 2
    return min(
        policy.max_facts,
        max(
            1,
            router_count,
            cross_check_count,
            both_count,
            scalar_enumeration_count,
            open_set_count,
            origin_pair_count,
            http_status_count,
            public_domain_count,
            checklist_count,
            checklist_matrix_count,
            source_split_count,
            map_rank_source_count,
            universal_count,
            enumeration_relation_count,
            source_additive_count,
            *explicit_counts,
            *explicit_source_counts,
        ),
    )


def maximum_fact_count(user_message: str, policy_version: str) -> int:
    """Bound explicitly scalar requests so models cannot broaden the question."""

    minimum = minimum_fact_count(user_message, policy_version)
    if _SCALAR_ONLY.search(user_message):
        return minimum
    return SearchPlanningPolicy(version=policy_version).max_facts


class PlanningModelGateway(Protocol):
    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult: ...


class IntentParser:
    def __init__(
        self,
        policy: SearchPlanningPolicy,
        *,
        allow_optional_facts: bool = False,
        allow_high_consequence: bool = False,
        minimum_facts: int = 1,
        maximum_facts: int | None = None,
    ) -> None:
        if not 1 <= minimum_facts <= policy.max_facts:
            raise ValueError("minimum_facts must fit within the planning policy")
        self._policy = policy
        self._allow_optional_facts = allow_optional_facts
        self._allow_high_consequence = allow_high_consequence
        self._minimum_facts = minimum_facts
        self._maximum_facts = maximum_facts or policy.max_facts
        if not minimum_facts <= self._maximum_facts <= policy.max_facts:
            raise ValueError("maximum_facts must fit the deterministic minimum")

    @staticmethod
    def _is_source_constraint_fact(raw: dict[str, Any]) -> bool:
        material = f"{raw.get('key', '')} {raw.get('description', '')}".casefold()
        return bool(
            re.search(
                r"(?:source|citation|bibliograph|reference|url|literature|\u6765\u6e90|\u5f15\u7528|\u94fe\u63a5|\u6587\u732e)"
                r"(?:[_ -]?(?:page|format|list|authority|literature|\u9875\u9762|\u6e05\u5355))?",
                material,
            )
            and re.search(
                r"(?:which|what|find|identify|document|cite|\u54ea|\u4ec0\u4e48|\u627e|\u5f15\u7528)",
                str(raw.get("description", "")),
                re.I,
            )
        )

    @staticmethod
    def _bounded_subject(raw: object, entity: str) -> str:
        subject = " ".join(str(raw or entity).split())
        if len(subject) > 48:
            subject = " ".join(entity.split())
        if len(subject) > 48:
            subject = subject[:48].rstrip(" -_/,:;")
        return subject

    def _consequence(self, raw: object) -> Consequence:
        consequence = Consequence(str(raw or "LOW").strip().upper())
        if consequence is Consequence.HIGH and not self._allow_high_consequence:
            return Consequence.MEDIUM
        return consequence

    @staticmethod
    def _normalize_meta_gap_fact(
        raw: dict[str, Any],
    ) -> tuple[str, str, tuple[str, ...]]:
        key = str(raw["key"])
        description = str(raw["description"])
        preferred = tuple(
            str(item) for item in raw.get("preferred_source_kinds", [])
        )
        meta_gap = bool(
            re.search(
                r"evidence[_ -]?gap|note the absence|absence of (?:such )?disclosure",
                f"{key} {description}",
                re.I,
            )
        )
        if not meta_gap:
            return key, description, preferred
        normalized_key = re.sub(
            r"evidence[_-]?gap",
            "independent_disclosure_check",
            key,
            flags=re.I,
        )
        if normalized_key == key:
            normalized_key = f"{key}_independent_disclosure_check"
        neutral_description = re.split(
            r"\bif\s+not\b",
            description,
            maxsplit=1,
            flags=re.I,
        )[0].strip().rstrip(". ")
        neutral_description = re.sub(
            r"\bpublic official sources?\b",
            "independent public sources",
            neutral_description,
            flags=re.I,
        )
        neutral_description = re.sub(
            r"\bpublic official disclosure\b",
            "independent public disclosure",
            neutral_description,
            flags=re.I,
        )
        if "independent" not in neutral_description.casefold():
            neutral_description = (
                "Whether independent public sources explicitly disclose: "
                f"{neutral_description}"
            )
        return normalized_key, neutral_description, ("independent",)

    def parse(self, text: str) -> NormalizedIntent:
        payload: dict[str, Any] = json.loads(text)
        supplied_facts = payload["facts"]
        if not isinstance(supplied_facts, list):
            raise ValueError("facts must be a bounded list")
        raw_facts = [
            raw
            for raw in supplied_facts
            if isinstance(raw, dict) and not self._is_source_constraint_fact(raw)
        ]
        if (
            not self._minimum_facts <= len(raw_facts) <= self._maximum_facts
        ):
            raise ValueError(
                "facts must satisfy the deterministic minimum and policy maximum"
            )
        facts_list: list[FactRequirement] = []
        for raw in raw_facts:
            key, description, preferred = self._normalize_meta_gap_fact(raw)
            facts_list.append(
                FactRequirement(
                    key=key,
                    fact_type=FactType(str(raw["fact_type"]).strip().lower()),
                    description=description,
                    subject=self._bounded_subject(
                        raw.get("subject"),
                        str(payload["entity"]),
                    ),
                    required=(
                        bool(raw.get("required", True))
                        if self._allow_optional_facts
                        else True
                    ),
                    freshness=Freshness(
                        str(raw.get("freshness", "STABLE")).strip().upper()
                    ),
                    consequence=self._consequence(
                        raw.get("consequence", "LOW")
                    ),
                    preferred_source_kinds=preferred,
                )
            )
        facts = tuple(facts_list)
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
            f" Return no more than {self._maximum_facts} facts."
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
        reviewed = reviewed_intent_template(user_message)
        if reviewed is not None:
            return reviewed
        minimum_facts = minimum_fact_count(user_message, self._policy.version)
        maximum_facts = maximum_fact_count(user_message, self._policy.version)
        system = (
            "Normalize the current request into a canonical entity and atomic fact "
            "requirements. Do not turn conversational filler into search terms. "
            "Never collapse separately requested subquestions into one background "
            "fact. When the request names N items and asks about each, emit at least "
            "N distinct facts; use one fact per named or enumerated item. Cross-check "
            "requests need separate facts for the source perspectives being checked. "
            "Describe each fact as a neutral evidence question or lookup target. Never "
            "pre-fill an answer, invent an example, or assert protocol constraints that "
            "are not stated in the current request. Preserve exact identifiers and "
            "terms the user explicitly asks to see. "
            "A request to cite a source is an evidence constraint, not a separate "
            "fact; do not emit citation-only, bibliography-only, or source-formatting "
            "facts. For unavailable, private, or future information, plan concrete "
            "public-disclosure checks for the requested source perspectives; never "
            "turn the instruction to report an evidence gap into an evidence-gap "
            "Fact. Keep identifiers that express one relationship together, such as "
            "a protocol version and the RFC that specifies it. Consequence describes "
            "real-world harm, not answer strictness: ordinary standards and software "
            "facts are LOW or MEDIUM, never HIGH. "
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
            f" Return no more than {maximum_facts} facts."
        )
        user = f"Current request:\n{user_message.strip()}"
        if allowed_conversation_summary.strip():
            user += (
                "\nAllowed context summary (resolve references only; never copy it as "
                f"a query suffix):\n{allowed_conversation_summary.strip()}"
            )
        high_consequence = (
            "high_consequence_cross_check"
            in AutomaticModeRouter(self._policy.version).route(user_message).reason_codes
        )
        parser = IntentParser(
            self._policy,
            allow_optional_facts=bool(self._OPTIONAL_REQUEST.search(user_message)),
            allow_high_consequence=high_consequence,
            minimum_facts=minimum_facts,
            maximum_facts=maximum_facts,
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
