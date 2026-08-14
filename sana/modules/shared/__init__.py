"""Dependency-free types shared by business modules."""

from sana.modules.shared.clock import Clock, FrozenClock, SystemClock
from sana.modules.shared.errors import ErrorCategory, InvariantViolation, TypedError
from sana.modules.shared.ids import IdFactory, RandomIdFactory, TraceContext
from sana.modules.shared.result import Result

__all__ = [
    "Clock",
    "ErrorCategory",
    "FrozenClock",
    "IdFactory",
    "InvariantViolation",
    "RandomIdFactory",
    "Result",
    "SystemClock",
    "TraceContext",
    "TypedError",
]
