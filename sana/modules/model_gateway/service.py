"""Unified retry, deadline, budget and structured-output handling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Generic, TypeVar

from sana.modules.model_gateway.domain import (
    MessageRole,
    ModelCallBudget,
    ModelDeadlineExceeded,
    ModelInvocationContext,
    ModelMessage,
    ModelOutputError,
    ModelRequest,
    ModelResult,
    ModelRole,
    OutputFormat,
    ProviderResponse,
    RedactedInvocationError,
    ThinkingMode,
)
from sana.modules.model_gateway.ports import (
    ModelInvocationAuditSink,
    ModelProvider,
    StructuredOutputParser,
)
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import ErrorCategory, TypedError


T = TypeVar("T")
# Keep enough wall-clock budget for audit finalization, structured fallback, and
# the enclosing Step transaction after an external provider stops responding.
_MODEL_COMPLETION_MARGIN_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class RoleConfig:
    provider: str
    model: str
    temperature: float = 0.0
    max_output_tokens: int = 1_024
    max_retries: int = 1
    request_timeout_seconds: float = 60.0
    output_format: OutputFormat = OutputFormat.TEXT
    thinking_mode: ThinkingMode = ThinkingMode.DISABLED
    prompt_template_version: str = "unspecified"
    parser_schema_version: str = "none"

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("Role provider and model cannot be empty")
        if self.max_output_tokens < 1 or self.max_retries < 0:
            raise ValueError("Role token and retry limits are invalid")
        if self.request_timeout_seconds <= 0:
            raise ValueError("Role request timeout must be positive")
        if not self.prompt_template_version.strip() or not self.parser_schema_version.strip():
            raise ValueError("Role template and parser versions cannot be empty")


class ModelGateway:
    def __init__(
        self,
        providers: dict[str, ModelProvider],
        role_configs: dict[ModelRole, RoleConfig],
        clock: Clock,
        audit_sink: ModelInvocationAuditSink | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._roles = dict(role_configs)
        self._clock = clock
        self._audit = audit_sink

    @staticmethod
    def _logical_call_key(
        context: ModelInvocationContext,
        request: ModelRequest,
        provider: str,
        call_no: int,
    ) -> str:
        safe_identity = {
            "tenant_id": str(context.tenant_id),
            "run_id": str(context.run_id),
            "step_id": str(context.step_id),
            "step_key": context.step_key,
            "role": request.role.value,
            "provider": provider,
            "model": request.model,
            "call_no": call_no,
            "output_format": request.output_format.value,
            "thinking_mode": request.thinking_mode.value,
            "prompt_template_version": request.prompt_template_version,
            "parser_schema_version": request.parser_schema_version,
            "input_refs": list(context.input_refs),
        }
        encoded = json.dumps(
            safe_identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

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
        invocation_context: ModelInvocationContext | None,
        call_no_start: int,
    ) -> tuple[ProviderResponse, int]:
        try:
            provider = self._providers[config.provider]
        except KeyError as exc:
            raise ValueError(f"Unknown model provider: {config.provider}") from exc
        calls = 0
        last_error: TypedError | None = None
        for attempt in range(config.max_retries + 1):
            remaining = self._remaining_seconds(deadline)
            if remaining <= _MODEL_COMPLETION_MARGIN_SECONDS:
                raise ModelDeadlineExceeded()
            call_no = call_no_start + calls + 1
            logical_call_key = ""
            if self._audit is not None:
                if invocation_context is None:
                    raise ValueError(
                        "Model invocation context is required when audit is enabled"
                    )
                logical_call_key = self._logical_call_key(
                    invocation_context,
                    request,
                    config.provider,
                    call_no,
                )
                reused = await self._audit.reuse(
                    invocation_context,
                    request,
                    provider=config.provider,
                    call_no=call_no,
                    logical_call_key=logical_call_key,
                    deadline=deadline,
                )
                if reused is not None:
                    return reused.response, calls
                reservation = await self._audit.start(
                    invocation_context,
                    request,
                    provider=config.provider,
                    call_no=call_no,
                    logical_call_key=logical_call_key,
                    deadline=deadline,
                )
            else:
                budget.reserve_call()
                reservation = None
            calls += 1
            try:
                # HTTP client timeouts are commonly per-I/O-operation rather than
                # total wall-clock limits.  The Gateway owns the absolute bound.
                provider_remaining = self._remaining_seconds(deadline)
                if provider_remaining <= _MODEL_COMPLETION_MARGIN_SECONDS:
                    raise ModelDeadlineExceeded()
                provider_timeout = min(
                    config.request_timeout_seconds,
                    provider_remaining - _MODEL_COMPLETION_MARGIN_SECONDS,
                )
                async with asyncio.timeout(provider_timeout):
                    response = await provider.invoke(
                        request,
                        timeout_seconds=provider_timeout,
                    )
            except asyncio.CancelledError as exc:
                if self._audit is not None and reservation is not None:
                    await asyncio.shield(
                        self._audit.fail(
                            reservation,
                            invocation_context,
                            RedactedInvocationError.from_exception(exc),
                        )
                    )
                raise
            except TimeoutError as exc:
                timeout_error = TypedError(
                    ErrorCategory.TRANSIENT,
                    "model_provider_timeout",
                    "Model provider exceeded its wall-clock budget",
                    retryable=True,
                    cause=exc,
                )
                if self._audit is not None and reservation is not None:
                    await self._audit.fail(
                        reservation,
                        invocation_context,
                        RedactedInvocationError.from_exception(timeout_error),
                    )
                last_error = timeout_error
                if attempt >= config.max_retries:
                    raise timeout_error from exc
                continue
            except Exception as exc:
                if self._audit is not None and reservation is not None:
                    await self._audit.fail(
                        reservation,
                        invocation_context,
                        RedactedInvocationError.from_exception(exc),
                    )
                if not isinstance(exc, TypedError):
                    raise
                last_error = exc
                if not exc.retryable or attempt >= config.max_retries:
                    raise
                continue
            if self._audit is not None and reservation is not None:
                await self._audit.complete(reservation, invocation_context, response)
            else:
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
        invocation_context: ModelInvocationContext | None = None,
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
            output_format=config.output_format,
            thinking_mode=config.thinking_mode,
            prompt_template_version=config.prompt_template_version,
            parser_schema_version=config.parser_schema_version,
        )
        response, calls = await self._invoke(
            request,
            config,
            deadline,
            budget,
            invocation_context,
            0,
        )
        if parser is None:
            return self._result(response, calls=calls, reused=calls == 0)

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
                output_format=config.output_format,
                thinking_mode=config.thinking_mode,
                prompt_template_version=config.prompt_template_version,
                parser_schema_version=config.parser_schema_version,
            )
            repaired_response, repair_calls = await self._invoke(
                repair_request,
                config,
                deadline,
                budget,
                invocation_context,
                max(calls, 1),
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
        reused: bool = False,
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
            reused=reused,
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
