"""A small explicit success/error value for module boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from sana.modules.shared.errors import TypedError


T = TypeVar("T")
U = TypeVar("U")
_MISSING = object()


@dataclass(frozen=True, slots=True, init=False)
class Result(Generic[T]):
    _value: T | object
    _error: TypedError | None

    def __init__(
        self,
        *,
        value: T | object = _MISSING,
        error: TypedError | None = None,
    ) -> None:
        if (value is _MISSING) == (error is None):
            raise ValueError("Result must contain exactly one of value or error")
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_error", error)

    @classmethod
    def ok(cls, value: T) -> "Result[T]":
        return cls(value=value)

    @classmethod
    def err(cls, error: TypedError) -> "Result[T]":
        return cls(error=error)

    @property
    def is_ok(self) -> bool:
        return self._error is None

    @property
    def is_err(self) -> bool:
        return self._error is not None

    def unwrap(self) -> T:
        if self._error is not None:
            raise self._error
        return cast(T, self._value)

    def unwrap_error(self) -> TypedError:
        if self._error is None:
            raise ValueError("Cannot unwrap an error from a successful Result")
        return self._error

    def map(self, transform: Callable[[T], U]) -> "Result[U]":
        if self._error is not None:
            return Result.err(self._error)
        return Result.ok(transform(cast(T, self._value)))
