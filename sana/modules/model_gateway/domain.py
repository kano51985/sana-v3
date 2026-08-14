"""Transport-neutral model roles, requests, results and usage budgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from sana.modules.shared.errors import ErrorCategory, TypedError


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

    def __post_init__(self) -> None:
        if not self.model.strip() or not self.messages:
            raise ValueError("Model and messages are required")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.prompt_tokens < 0 or self.completion_tokens < 0:
            raise ValueError("Token usage cannot be negative")
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    model: str
    parsed: Any = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider_calls: int = 1
    repaired: bool = False


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
