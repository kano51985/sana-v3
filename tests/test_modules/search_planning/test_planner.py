import json
from datetime import datetime, timedelta, timezone

import pytest

from sana.modules.model_gateway.domain import ModelCallBudget, ModelResult
from sana.modules.search_planning.domain import NormalizedIntent
from sana.modules.search_planning.planner import SearchPlanner


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
