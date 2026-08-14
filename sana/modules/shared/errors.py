"""Stable error vocabulary shared across transports and workers."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


class ErrorCategory(StrEnum):
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    BUDGET = "BUDGET"
    CONTENT = "CONTENT"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"


_RETRYABLE_BY_DEFAULT = frozenset({ErrorCategory.TRANSIENT})


class TypedError(Exception):
    """An operational error safe to persist and classify across processes."""

    def __init__(
        self,
        category: ErrorCategory,
        code: str,
        message: str,
        *,
        retryable: bool | None = None,
        details: Mapping[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        normalized_code = code.strip()
        normalized_message = message.strip()
        if not normalized_code:
            raise ValueError("TypedError code cannot be empty")
        if not normalized_message:
            raise ValueError("TypedError message cannot be empty")
        super().__init__(normalized_message)
        self.category = ErrorCategory(category)
        self.code = normalized_code
        self.message = normalized_message
        self.retryable = (
            self.category in _RETRYABLE_BY_DEFAULT
            if retryable is None
            else bool(retryable)
        )
        self.details = MappingProxyType(dict(details or {}))
        self.cause = cause

    def __str__(self) -> str:
        return f"[{self.category}:{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


class InvariantViolation(TypedError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invariant_violation",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(
            ErrorCategory.PERMANENT,
            code,
            message,
            retryable=False,
            details=details,
        )
