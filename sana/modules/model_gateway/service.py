"""Unified retry, deadline, budget and structured-output handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, TypeVar

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelDeadlineExceeded,
    ModelMessage,
    ModelOutputError,
    ModelRequest,
    ModelResult,
    ModelRole,
    ProviderResponse,
)
from sana.modules.model_gateway.ports import ModelProvider, StructuredOutputParser
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import TypedError


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RoleConfig:
    provider: str
    model: str
    temperature: float = 0.0
    max_output_tokens: int = 1_024
    max_retries: int = 1
    request_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("Role provider and model cannot be empty")
        if self.max_output_tokens < 1 or self.max_retries < 0:
            raise ValueError("Role token and retry limits are invalid")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Role request timeout must be positive")


class ModelGateway:
    def __init__(
        self,
        providers: dict[str, ModelProvider],
        role_configs: dict[ModelRole, RoleConfig],
        clock: Clock,
    ) -> None:
        self._providers = dict(providers)
        self._roles = dict(role_configs)
        self._clock = clock

    def _remaining_seconds(self, deadline: datetime) -> float:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("Model deadline must be timezone-aware")
        remaining = (deadline - self._clock.now()).total_seconds()
        if remaining <= 0:
            raise ModelDeadlineExceeded()
        return remaining

    async def _invoke(
        self,
        request: ModelRequest,
        config: RoleConfig,
        deadline: datetime,
        budget: ModelCallBudget,
    ) -> tuple[ProviderResponse, int]:
        try:
            provider = self._providers[config.provider]
        except KeyError as exc:
            raise ValueError(f"Unknown model provider: {config.provider}") from exc
        calls = 0
        last_error: TypedError | None = None
        for attempt in range(config.max_retries + 1):
            remaining = self._remaining_seconds(deadline)
            budget.reserve_call()
            calls += 1
            try:
                response = await provider.invoke(
                    request,
                    timeout_seconds=min(config.request_timeout_seconds, remaining),
                )
            except TypedError as exc:
                last_error = exc
                if not exc.retryable or attempt >= config.max_retries:
                    raise
                continue
            budget.record_tokens(response.prompt_tokens, response.completion_tokens)
            return response, calls
        assert last_error is not None
        raise last_error

    async def generate(
        self,
        role: ModelRole,
        messages: tuple[ModelMessage, ...],
        *,
        deadline: datetime,
        budget: ModelCallBudget,
        parser: StructuredOutputParser[T] | None = None,
    ) -> ModelResult:
        try:
            config = self._roles[role]
        except KeyError as exc:
            raise ValueError(f"Model role is not configured: {role}") from exc
        request = ModelRequest(
            role=role,
            model=config.model,
            messages=messages,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
        )
        response, calls = await self._invoke(request, config, deadline, budget)
        if parser is None:
            return self._result(response, calls=calls)

        try:
            parsed = parser.parse(response.text)
        except (ValueError, TypeError, KeyError) as first_error:
            repair_request = ModelRequest(
                role=role,
                model=config.model,
                messages=(
                    *messages,
                    ModelMessage(MessageRole.ASSISTANT, response.text or "<empty>"),
                    ModelMessage(
                        MessageRole.USER,
                        parser.repair_instruction(first_error),
                    ),
                ),
                temperature=0.0,
                max_output_tokens=config.max_output_tokens,
            )
            repaired_response, repair_calls = await self._invoke(
                repair_request,
                config,
                deadline,
                budget,
            )
            calls += repair_calls
            try:
                parsed = parser.parse(repaired_response.text)
            except (ValueError, TypeError, KeyError) as second_error:
                raise ModelOutputError(
                    f"Structured output remained invalid after one repair: {second_error}"
                ) from second_error
            return self._result(
                repaired_response,
                parsed=parsed,
                calls=calls,
                repaired=True,
                prompt_tokens=(
                    response.prompt_tokens + repaired_response.prompt_tokens
                ),
                completion_tokens=(
                    response.completion_tokens + repaired_response.completion_tokens
                ),
            )
        return self._result(response, parsed=parsed, calls=calls)

    @staticmethod
    def _result(
        response: ProviderResponse,
        *,
        parsed: Any = None,
        calls: int,
        repaired: bool = False,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> ModelResult:
        return ModelResult(
            text=response.text,
            model=response.model,
            parsed=parsed,
            prompt_tokens=(
                response.prompt_tokens if prompt_tokens is None else prompt_tokens
            ),
            completion_tokens=(
                response.completion_tokens
                if completion_tokens is None
                else completion_tokens
            ),
            provider_calls=calls,
            repaired=repaired,
        )


class FakeModelGateway(Generic[T]):
    """Offline-by-default scripted gateway for tests and workflow simulations."""

    def __init__(self, results: list[ModelResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[ModelRole, tuple[ModelMessage, ...]]] = []

    async def generate(
        self,
        role: ModelRole,
        messages: tuple[ModelMessage, ...],
        **_: Any,
    ) -> ModelResult:
        self.calls.append((role, messages))
        if not self._results:
            raise AssertionError("FakeModelGateway has no scripted result")
        return self._results.pop(0)
