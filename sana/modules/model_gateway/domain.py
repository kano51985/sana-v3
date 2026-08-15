"""Transport-neutral model roles, requests, results and usage budgets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import TraceContext


class ModelRole(StrEnum):
    ROUTER = "ROUTER"
    PLANNER = "PLANNER"
    VERIFIER = "VERIFIER"
    SYNTHESIZER = "SYNTHESIZER"
    CONVERSATION = "CONVERSATION"
    MEMORY = "MEMORY"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class OutputFormat(StrEnum):
    TEXT = "text"
    JSON_OBJECT = "json_object"


class ThinkingMode(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"


class ModelInvocationStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    REUSED = "REUSED"


class BillingDisposition(StrEnum):
    NOT_BILLED = "NOT_BILLED"
    BILLED = "BILLED"
    POSSIBLY_BILLED = "POSSIBLY_BILLED"


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: MessageRole
    content: str

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Model message content cannot be empty")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    role: ModelRole
    model: str
    messages: tuple[ModelMessage, ...]
    temperature: float
    max_output_tokens: int
    output_format: OutputFormat = OutputFormat.TEXT
    thinking_mode: ThinkingMode = ThinkingMode.DISABLED
    prompt_template_version: str = "unspecified"
    parser_schema_version: str = "none"

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.messages:
            raise ValueError("Model and messages are required")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if not self.prompt_template_version.strip():
            raise ValueError("prompt_template_version cannot be empty")
        if not self.parser_schema_version.strip():
            raise ValueError("parser_schema_version cannot be empty")


@dataclass(frozen=True, slots=True)
class ModelInvocationContext:
    tenant_id: UUID
    run_id: UUID
    step_id: UUID
    step_key: str
    attempt_id: UUID
    attempt_no: int
    trace_context: TraceContext
    input_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.step_key.strip():
            raise ValueError("Model invocation step key cannot be empty")
        if self.attempt_no < 1:
            raise ValueError("Model invocation attempt number must be positive")
        if any(not value.strip() for value in self.input_refs):
            raise ValueError("Model invocation input references cannot be empty")


@dataclass(frozen=True, slots=True)
class ModelInvocationReservation:
    id: UUID
    call_no: int
    logical_call_key: str

    def __post_init__(self) -> None:
        if self.call_no < 1 or not self.logical_call_key.strip():
            raise ValueError("Model invocation reservation is invalid")


@dataclass(frozen=True, slots=True)
class ReusedModelResponse:
    response: "ProviderResponse"
    source_invocation_id: UUID


@dataclass(frozen=True, slots=True)
class RedactedInvocationError:
    category: str
    code: str
    retryable: bool

    @classmethod
    def from_exception(cls, error: BaseException) -> "RedactedInvocationError":
        if isinstance(error, TypedError):
            return cls(error.category.value, error.code, error.retryable)
        if isinstance(error, asyncio.CancelledError):
            return cls(ErrorCategory.CANCELLED.value, "model_call_cancelled", False)
        return cls(ErrorCategory.INFRASTRUCTURE.value, "model_provider_error", False)


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_id: str | None = None

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("Token usage cannot be negative")
        if self.response_id is not None and not self.response_id.strip():
            raise ValueError("response_id cannot be blank")


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    model: str
    parsed: Any = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_calls: int = 1
    repaired: bool = False
    reused: bool = False


class ModelBudgetExceeded(TypedError):
    def __init__(self, limit: str, used: int, maximum: int) -> None:
        super().__init__(
            ErrorCategory.BUDGET,
            "model_budget_exceeded",
            f"Model budget exceeded: {limit}",
            retryable=False,
            details={"limit": limit, "used": used, "maximum": maximum},
        )


@dataclass(slots=True)
class ModelCallBudget:
    max_calls: int
    max_total_tokens: int
    used_calls: int = 0
    used_prompt_tokens: int = 0
    used_completion_tokens: int = 0

    def __post_init__(self) -> None:
        if self.max_calls < 0 or self.max_total_tokens < 0:
            raise ValueError("Model budget limits cannot be negative")

    @property
    def used_total_tokens(self) -> int:
        return self.used_prompt_tokens + self.used_completion_tokens

    def reserve_call(self) -> None:
        next_calls = self.used_calls + 1
        if next_calls > self.max_calls:
            raise ModelBudgetExceeded("calls", next_calls, self.max_calls)
        self.used_calls = next_calls

    def record_tokens(self, prompt_tokens: int, completion_tokens: int) -> None:
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("Token usage cannot be negative")
        next_total = self.used_total_tokens + prompt_tokens + completion_tokens
        if next_total > self.max_total_tokens:
            raise ModelBudgetExceeded("tokens", next_total, self.max_total_tokens)
        self.used_prompt_tokens += prompt_tokens
        self.used_completion_tokens += completion_tokens


class ModelDeadlineExceeded(TypedError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCategory.BUDGET,
            "model_deadline_exceeded",
            "Model call cannot start after the run deadline",
            retryable=False,
        )


class ModelOutputError(TypedError):
    def __init__(self, message: str) -> None:
        super().__init__(
            ErrorCategory.MODEL_OUTPUT,
            "invalid_structured_output",
            message,
            retryable=False,
        )
