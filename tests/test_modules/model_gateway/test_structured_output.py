import json
from datetime import datetime, timedelta, timezone

import pytest

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelMessage,
    ModelOutputError,
    ModelRole,
    ProviderResponse,
)
from sana.modules.model_gateway.service import ModelGateway, RoleConfig
from sana.modules.shared.clock import FrozenClock


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class ScriptedProvider:
    def __init__(self, texts) -> None:
        self.texts = list(texts)
        self.requests = []

    async def invoke(self, request, *, timeout_seconds):
        self.requests.append(request)
        return ProviderResponse(self.texts.pop(0), request.model, 1, 1)


class RequiredModeParser:
    def parse(self, text: str):
        value = json.loads(text)
        if value.get("mode") not in {"FAST", "RESEARCH"}:
            raise ValueError("mode is required")
        return value

    def repair_instruction(self, error: Exception) -> str:
        return f"Return one JSON object with FAST or RESEARCH mode. Error: {error}"


def make_gateway(provider):
    return ModelGateway(
        {"fake": provider},
        {ModelRole.ROUTER: RoleConfig("fake", "router", max_retries=0)},
        FrozenClock(NOW),
    )


@pytest.mark.asyncio
async def test_invalid_structure_is_repaired_at_most_once_and_is_budgeted() -> None:
    provider = ScriptedProvider(["not-json", '{"mode":"RESEARCH"}'])
    budget = ModelCallBudget(2, 100)

    result = await make_gateway(provider).generate(
        ModelRole.ROUTER,
        (ModelMessage(MessageRole.USER, "compare"),),
        deadline=NOW + timedelta(seconds=5),
        budget=budget,
        parser=RequiredModeParser(),
    )

    assert result.parsed == {"mode": "RESEARCH"}
    assert result.repaired is True
    assert result.provider_calls == 2
    assert result.prompt_tokens == 2
    assert result.completion_tokens == 2
    assert budget.used_calls == 2
    assert "Return one JSON object" in provider.requests[1].messages[-1].content


@pytest.mark.asyncio
async def test_second_invalid_structure_fails_without_third_call() -> None:
    provider = ScriptedProvider(["bad", "still bad"])

    with pytest.raises(ModelOutputError, match="after one repair"):
        await make_gateway(provider).generate(
            ModelRole.ROUTER,
            (ModelMessage(MessageRole.USER, "compare"),),
            deadline=NOW + timedelta(seconds=5),
            budget=ModelCallBudget(5, 100),
            parser=RequiredModeParser(),
        )
    assert len(provider.requests) == 2
