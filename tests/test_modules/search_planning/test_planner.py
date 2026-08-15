import json
from datetime import datetime, timedelta, timezone

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
    minimum_fact_count,
)
from sana.modules.search_planning.policy import SearchPlanningPolicy


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class ParsingGateway:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    async def generate(self, role, messages, **kwargs):
        self.calls.append((role, messages, kwargs))
        parser = kwargs["parser"]
        text = json.dumps(self.payload, ensure_ascii=False)
        return ModelResult(text=text, model="fake", parsed=parser.parse(text))


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
