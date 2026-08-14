"""Reusable transition validation for orchestration aggregates."""

from __future__ import annotations

from enum import Enum
from typing import Mapping, TypeVar

from sana.modules.shared.errors import InvariantViolation


State = TypeVar("State", bound=Enum)


def assert_transition(
    current: State,
    target: State,
    allowed: Mapping[State, frozenset[State]],
    *,
    entity: str,
) -> None:
    if target not in allowed.get(current, frozenset()):
        raise InvariantViolation(
            f"Illegal {entity} transition: {current.value} -> {target.value}",
            code="illegal_state_transition",
            details={
                "entity": entity,
                "current": current.value,
                "target": target.value,
            },
        )
