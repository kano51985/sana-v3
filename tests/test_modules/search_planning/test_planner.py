import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sana.modules.model_gateway.domain import ModelCallBudget, ModelResult
from sana.modules.search_planning.domain import (
    Consequence,
    FactType,
    Freshness,
    NormalizedIntent,
)
from sana.modules.search_planning.planner import (
    IntentParser,
    SearchPlanner,
    maximum_fact_count,
    minimum_fact_count,
)
from sana.modules.search_planning.policy import SearchPlanningPolicy


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
SHADOW_MANIFEST = (
    Path(__file__).parents[3] / "evals" / "shadow" / "cases-v1.jsonl"
)


class ParsingGateway:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    async def generate(self, role, messages, **kwargs):
        self.calls.append((role, messages, kwargs))
        parser = kwargs["parser"]
        text = json.dumps(self.payload, ensure_ascii=False)
        return ModelResult(text=text, model="fake", parsed=parser.parse(text))


class ForbiddenGateway:
    async def generate(self, *args, **kwargs):
        raise AssertionError("reviewed intent templates must bypass model planning")


@pytest.mark.asyncio
async def test_planner_makes_one_primary_call_and_returns_semantic_facts() -> None:
    gateway = ParsingGateway(
        {
            "entity": "Apex Legends",
            "aliases": ["apex"],
            "locale": "zh-CN",
            "facts": [
                {
                    "key": "version",
                    "fact_type": "version",
                    "description": "当前版本",
                    "subject": "Apex Legends",
                    "freshness": "CURRENT",
                    "consequence": "LOW"
                }
            ]
        }
    )
    planner = SearchPlanner(gateway)

    intent = await planner.plan(
        "sana，Apex 当前是什么版本？",
        allowed_conversation_summary="用户之前玩过这个游戏",
        deadline=NOW + timedelta(seconds=2),
        model_budget=ModelCallBudget(2, 1000),
    )

    assert isinstance(intent, NormalizedIntent)
    assert intent.entity == "Apex Legends"
    assert len(gateway.calls) == 1
    assert "never copy it as a query suffix" in gateway.calls[0][1][1].content
    assert "Never collapse separately requested subquestions" in gateway.calls[0][1][0].content
    assert "citation-only" in gateway.calls[0][1][0].content
    assert "never turn the instruction to report an evidence gap" in gateway.calls[0][1][0].content
    assert "ordinary standards and software facts" in gateway.calls[0][1][0].content
    assert "required=true" in gateway.calls[0][1][0].content


@pytest.mark.asyncio
async def test_reviewed_standards_request_skips_model_planning() -> None:
    budget = ModelCallBudget(2, 1000)

    intent = await SearchPlanner(ForbiddenGateway()).plan(
        "Under HTTP semantics, is GET both safe and idempotent?",
        deadline=NOW + timedelta(seconds=2),
        model_budget=budget,
    )

    assert [fact.key for fact in intent.facts] == [
        "http_get_safe",
        "http_get_idempotent",
    ]
    assert budget.used_calls == 0


def test_intent_parser_normalizes_enum_casing_but_keeps_schema_strict() -> None:
    parser = IntentParser(SearchPlanningPolicy())

    intent = parser.parse(
        json.dumps(
            {
                "entity": "DeepSeek V4",
                "aliases": [],
                "locale": "zh-CN",
                "facts": [
                    {
                        "key": "current-version",
                        "fact_type": "VERSION",
                        "description": "current version",
                        "subject": "DeepSeek V4",
                        "required": True,
                        "freshness": "current",
                        "consequence": "medium",
                        "preferred_source_kinds": ["official"],
                    }
                ],
            }
        )
    )

    assert intent.facts[0].fact_type is FactType.VERSION
    assert intent.facts[0].freshness is Freshness.CURRENT
    assert intent.facts[0].consequence is Consequence.MEDIUM


def test_only_policy_recognized_high_stakes_requests_can_keep_high_consequence() -> None:
    payload = json.dumps(
        {
            "entity": "TLS 1.3",
            "facts": [
                {
                    "key": "rfc",
                    "fact_type": "background",
                    "description": "RFC for TLS 1.3",
                    "subject": "TLS 1.3",
                    "consequence": "HIGH",
                }
            ],
        }
    )

    ordinary = IntentParser(SearchPlanningPolicy()).parse(payload)
    high_stakes = IntentParser(
        SearchPlanningPolicy(),
        allow_high_consequence=True,
    ).parse(payload)

    assert ordinary.facts[0].consequence is Consequence.MEDIUM
    assert high_stakes.facts[0].consequence is Consequence.HIGH


def test_repair_instruction_names_exact_enum_contract() -> None:
    instruction = IntentParser(SearchPlanningPolicy()).repair_instruction(
        ValueError("bad payload")
    )

    assert "STABLE, RECENT, or CURRENT" in instruction
    assert "LOW, MEDIUM, or HIGH" in instruction
    assert "bad payload" not in instruction


def test_model_cannot_silently_mark_requested_fact_optional() -> None:
    payload = json.dumps(
        {
            "entity": "Apex Legends",
            "aliases": [],
            "locale": "zh-CN",
            "facts": [
                {
                    "key": "team_meta",
                    "fact_type": "team_meta",
                    "description": "当前阵容建议",
                    "subject": "Apex Legends",
                    "required": False,
                }
            ],
        }
    )

    required_intent = IntentParser(SearchPlanningPolicy()).parse(payload)
    optional_intent = IntentParser(
        SearchPlanningPolicy(),
        allow_optional_facts=True,
    ).parse(payload)

    assert required_intent.facts[0].required is True
    assert optional_intent.facts[0].required is False


def test_minimum_fact_count_preserves_explicit_enumeration() -> None:
    message = "请研究 Git 对象模型：列出四种对象类型，并分别说明用途"

    assert minimum_fact_count(message, "search-v12") == 4


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Is GET both safe and idempotent?", 2),
        ("What are the three lowercase JSON literals?", 3),
        (
            "Which port does DNS use, and over which two transport protocols?",
            3,
        ),
        (
            "研究 PostgreSQL 当前仍受支持的主版本和停止支持日期。",
            5,
        ),
    ],
)
def test_minimum_fact_count_handles_structured_multi_part_requests(
    message: str,
    expected: int,
) -> None:
    assert minimum_fact_count(message, "search-v12") == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Explain the three properties in the CAP theorem and the tradeoff "
            "asserted during a network partition.",
            4,
        ),
        (
            "Research the current Apex Legends release and the latest official "
            "balance changes for Bloodhound. Separate confirmed patch facts from "
            "community interpretation and cite both source classes.",
            4,
        ),
        (
            "Sana, research the current ranked rules, map rotation, and two "
            "evidence-backed team-composition perspectives.",
            4,
        ),
        (
            "Prove one universally best Apex Legends team composition for every "
            "rank, map, region, and player skill level.",
            3,
        ),
    ],
)
def test_shadow_campaign_prompts_keep_their_semantic_fact_floor(
    message: str,
    expected: int,
) -> None:
    assert minimum_fact_count(message, "search-v12") == expected


def test_scalar_request_caps_model_planning_at_one_fact() -> None:
    message = (
        "DeepSeek 官方当前列出的 deepseek-v4-flash 每百万输出 token 的美元价格"
        "是多少？只回答该价格并引用官方定价页。"
    )

    assert minimum_fact_count(message, "search-v12") == 1
    assert maximum_fact_count(message, "search-v12") == 1


def test_intent_parser_bounds_long_subject_without_dropping_valid_facts() -> None:
    parser = IntentParser(SearchPlanningPolicy(), minimum_facts=4)
    payload = {
        "entity": "Apex Legends",
        "facts": [
            {
                "key": "map_rotation",
                "fact_type": "current_value",
                "description": "current official map rotation",
                "subject": "Apex Legends",
            },
            {
                "key": "ranked_rules",
                "fact_type": "current_value",
                "description": "current ranked rules",
                "subject": "Apex Legends",
            },
            {
                "key": "team_perspective_one",
                "fact_type": "team_meta",
                "description": "first evidence-backed team perspective",
                "subject": (
                    "Apex Legends team composition perspective backed by one "
                    "current analysis source"
                ),
            },
            {
                "key": "team_perspective_two",
                "fact_type": "team_meta",
                "description": "second evidence-backed team perspective",
                "subject": "Apex Legends",
            },
        ],
    }

    intent = parser.parse(json.dumps(payload))

    assert len(intent.facts) == 4
    assert intent.facts[2].subject == "Apex Legends"


def test_intent_parser_removes_citation_only_fact_but_keeps_cap_semantics() -> None:
    parser = IntentParser(SearchPlanningPolicy(), minimum_facts=4)
    payload = {
        "entity": "CAP theorem",
        "facts": [
            {
                "key": name.casefold().replace(" ", "_"),
                "fact_type": "background",
                "description": f"Explain {name}",
                "subject": "CAP theorem",
            }
            for name in (
                "Consistency",
                "Availability",
                "Partition tolerance",
                "Partition tradeoff",
            )
        ] + [
            {
                "key": "cap_original_literature",
                "fact_type": "background",
                "description": "Identify and cite the original authoritative literature",
                "subject": "CAP theorem",
            }
        ],
    }

    intent = parser.parse(json.dumps(payload))

    assert len(intent.facts) == 4
    assert {fact.key for fact in intent.facts} == {
        "consistency",
        "availability",
        "partition_tolerance",
        "partition_tradeoff",
    }


def test_planner_floor_covers_every_versioned_shadow_case() -> None:
    cases = [
        json.loads(line)
        for line in SHADOW_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    mismatches = {
        case["id"]: (
            minimum_fact_count(case["prompt"], "search-v12"),
            case["minimum_required_facts"],
        )
        for case in cases
        if minimum_fact_count(case["prompt"], "search-v12")
        < case["minimum_required_facts"]
    }

    assert mismatches == {}


def test_intent_parser_rejects_semantically_incomplete_fact_list() -> None:
    parser = IntentParser(SearchPlanningPolicy(), minimum_facts=4)
    payload = json.dumps(
        {
            "entity": "Git",
            "aliases": [],
            "locale": "zh-CN",
            "facts": [
                {
                    "key": "object_model",
                    "fact_type": "background",
                    "description": "Git object model",
                    "subject": "Git",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="deterministic minimum"):
        parser.parse(payload)


def test_meta_evidence_gap_is_normalized_to_independent_disclosure_check() -> None:
    parser = IntentParser(SearchPlanningPolicy(), minimum_facts=2)
    intent = parser.parse(
        json.dumps(
            {
                "entity": "OpenAI next unreleased model",
                "facts": [
                    {
                        "key": "private_weights_public",
                        "fact_type": "current_value",
                        "description": "Whether official sources disclose the weights",
                        "subject": "OpenAI next unreleased model",
                    },
                    {
                        "key": "private_weights_evidence_gap",
                        "fact_type": "background",
                        "description": (
                            "Is there any public official disclosure of the parameter "
                            "weights? If not, note the absence of such disclosure."
                        ),
                        "subject": "OpenAI next unreleased model",
                    },
                ],
            }
        )
    )

    normalized = intent.facts[1]
    assert normalized.key == "private_weights_independent_disclosure_check"
    assert "independent public disclosure" in normalized.description
    assert "If not" not in normalized.description
    assert normalized.preferred_source_kinds == ("independent",)
