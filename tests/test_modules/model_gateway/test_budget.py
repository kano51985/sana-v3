import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelBudgetExceeded,
    ModelCallBudget,
    ModelDeadlineExceeded,
    ModelMessage,
    ModelRole,
    ProviderResponse,
)
from sana.modules.model_gateway.service import ModelGateway, RoleConfig
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import ErrorCategory, TypedError


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
MESSAGES = (ModelMessage(MessageRole.USER, "hello"),)


class ScriptedProvider:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.timeouts = []

    async def invoke(self, request, *, timeout_seconds):
        self.timeouts.append(timeout_seconds)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class HangingProvider:
    async def invoke(self, request, *, timeout_seconds):
        del request, timeout_seconds
        await asyncio.sleep(1)
        return ProviderResponse("late", "model")


def make_gateway(provider, *, retries=1, timeout=60.0):
    return ModelGateway(
        {"fake": provider},
        {
            ModelRole.PLANNER: RoleConfig(
                "fake",
                "model",
                max_retries=retries,
                request_timeout_seconds=timeout,
            )
        },
        FrozenClock(NOW),
    )


@pytest.mark.asyncio
async def test_absolute_deadline_caps_provider_timeout() -> None:
    provider = ScriptedProvider([ProviderResponse("ok", "model", 10, 5)])
    gateway = make_gateway(provider, timeout=60)
    budget = ModelCallBudget(max_calls=2, max_total_tokens=100)

    result = await gateway.generate(
        ModelRole.PLANNER,
        MESSAGES,
        deadline=NOW + timedelta(seconds=3),
        budget=budget,
    )

    assert result.text == "ok"
    assert provider.timeouts == [1.0]
    assert budget.used_calls == 1
    assert budget.used_total_tokens == 15


@pytest.mark.asyncio
async def test_gateway_enforces_total_provider_wall_clock_timeout() -> None:
    gateway = make_gateway(HangingProvider(), retries=0, timeout=0.01)

    with pytest.raises(TypedError) as captured:
        await gateway.generate(
            ModelRole.PLANNER,
            MESSAGES,
            deadline=NOW + timedelta(seconds=10),
            budget=ModelCallBudget(1, 100),
        )

    assert captured.value.code == "model_provider_timeout"


@pytest.mark.asyncio
async def test_no_provider_call_starts_after_deadline() -> None:
    provider = ScriptedProvider([ProviderResponse("never", "model")])
    gateway = make_gateway(provider)

    with pytest.raises(ModelDeadlineExceeded):
        await gateway.generate(
            ModelRole.PLANNER,
            MESSAGES,
            deadline=NOW,
            budget=ModelCallBudget(1, 100),
        )
    assert provider.timeouts == []


@pytest.mark.asyncio
async def test_retry_consumes_same_call_budget() -> None:
    transient = TypedError(
        ErrorCategory.TRANSIENT,
        "temporary",
        "try again",
        retryable=True,
    )
    provider = ScriptedProvider([transient, ProviderResponse("ok", "model")])
    gateway = make_gateway(provider, retries=1)
    budget = ModelCallBudget(max_calls=1, max_total_tokens=100)

    with pytest.raises(ModelBudgetExceeded):
        await gateway.generate(
            ModelRole.PLANNER,
            MESSAGES,
            deadline=NOW + timedelta(seconds=10),
            budget=budget,
        )
    assert budget.used_calls == 1
    assert len(provider.timeouts) == 1


@pytest.mark.asyncio
async def test_permanent_provider_error_is_not_retried() -> None:
    permanent = TypedError(
        ErrorCategory.PERMANENT,
        "bad_request",
        "do not retry",
        retryable=False,
    )
    provider = ScriptedProvider([permanent, ProviderResponse("unused", "model")])

    with pytest.raises(TypedError, match="do not retry"):
        await make_gateway(provider, retries=2).generate(
            ModelRole.PLANNER,
            MESSAGES,
            deadline=NOW + timedelta(seconds=10),
            budget=ModelCallBudget(5, 100),
        )
    assert len(provider.timeouts) == 1
