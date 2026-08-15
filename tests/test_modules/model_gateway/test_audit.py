from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelInvocationContext,
    ModelInvocationReservation,
    ModelMessage,
    ModelResult,
    ModelRole,
    ProviderResponse,
    ReusedModelResponse,
)
from sana.modules.model_gateway.service import ModelGateway, RoleConfig
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, request, *, timeout_seconds):
        self.calls += 1
        return ProviderResponse("{\"ok\":true}", request.model, 4, 2)


class CancelledProvider(Provider):
    async def invoke(self, request, *, timeout_seconds):
        self.calls += 1
        raise asyncio.CancelledError()


class Audit:
    def __init__(self, reused=None) -> None:
        self.reused = reused
        self.started = []
        self.completed = []
        self.failed = []

    async def reuse(self, context, request, **values):
        self.reuse_values = values
        return self.reused

    async def start(self, context, request, **values):
        self.started.append(values)
        return ModelInvocationReservation(uuid4(), values["call_no"], values["logical_call_key"])

    async def complete(self, reservation, context, response):
        self.completed.append((reservation, response))

    async def fail(self, reservation, context, error):
        self.failed.append((reservation, error))


def context() -> ModelInvocationContext:
    return ModelInvocationContext(
        uuid4(),
        uuid4(),
        uuid4(),
        "verify",
        uuid4(),
        1,
        TraceContext.create(),
        ("artifact-sha256:abc",),
    )


def gateway(provider, audit):
    return ModelGateway(
        {"fake": provider},
        {ModelRole.VERIFIER: RoleConfig("fake", "model", max_retries=0)},
        FrozenClock(NOW),
        audit,
    )


@pytest.mark.asyncio
async def test_audit_reservation_is_the_provider_call_budget_authority() -> None:
    provider = Provider()
    audit = Audit()
    memory_budget = ModelCallBudget(0, 0)

    result = await gateway(provider, audit).generate(
        ModelRole.VERIFIER,
        (ModelMessage(MessageRole.USER, "private prompt"),),
        deadline=NOW + timedelta(seconds=3),
        budget=memory_budget,
        invocation_context=context(),
    )

    assert result.provider_calls == 1
    assert provider.calls == 1
    assert len(audit.started) == len(audit.completed) == 1
    assert memory_budget.used_calls == 0
    assert memory_budget.used_total_tokens == 0
    assert len(audit.started[0]["logical_call_key"]) == 64
    assert "private prompt" not in audit.started[0]["logical_call_key"]


@pytest.mark.asyncio
async def test_completed_artifact_reuse_never_calls_provider_or_consumes_budget() -> None:
    provider = Provider()
    audit = Audit(ReusedModelResponse(ProviderResponse("reused", "model", 8, 3), uuid4()))

    result = await gateway(provider, audit).generate(
        ModelRole.VERIFIER,
        (ModelMessage(MessageRole.USER, "same logical input"),),
        deadline=NOW + timedelta(seconds=3),
        budget=ModelCallBudget(0, 0),
        invocation_context=context(),
    )

    assert result == ModelResult(
        "reused",
        "model",
        prompt_tokens=8,
        completion_tokens=3,
        provider_calls=0,
        reused=True,
    )
    assert provider.calls == 0
    assert audit.started == []


@pytest.mark.asyncio
async def test_cancelled_provider_call_is_sealed_before_cancellation_propagates() -> None:
    provider = CancelledProvider()
    audit = Audit()

    with pytest.raises(asyncio.CancelledError):
        await gateway(provider, audit).generate(
            ModelRole.VERIFIER,
            (ModelMessage(MessageRole.USER, "private prompt"),),
            deadline=NOW + timedelta(seconds=3),
            budget=ModelCallBudget(0, 0),
            invocation_context=context(),
        )

    assert provider.calls == 1
    assert len(audit.started) == len(audit.failed) == 1
    assert audit.failed[0][1].code == "model_call_cancelled"
