"""Explicit FAST Step-to-operation wiring for worker composition roots."""

from __future__ import annotations

from dataclasses import dataclass

from sana.modules.orchestration.domain import StepType
from sana.modules.orchestration.step_handlers.base import (
    BoundedStepHandler,
    StepHandlerRegistry,
    StepOperation,
)


@dataclass(frozen=True, slots=True)
class FastStepOperations:
    route: StepOperation
    plan: StepOperation
    discovery: StepOperation
    select: StepOperation
    fetch: StepOperation
    extract: StepOperation
    verify: StepOperation
    synthesize: StepOperation


def build_fast_handler_registry(
    operations: FastStepOperations,
) -> StepHandlerRegistry:
    registry = StepHandlerRegistry()
    wiring = {
        StepType.ROUTE: operations.route,
        StepType.PLAN: operations.plan,
        StepType.DISCOVERY: operations.discovery,
        StepType.SELECT: operations.select,
        StepType.FETCH: operations.fetch,
        StepType.EXTRACT: operations.extract,
        StepType.VERIFY: operations.verify,
        StepType.SYNTHESIZE: operations.synthesize,
    }
    for step_type, operation in wiring.items():
        registry.register(step_type, BoundedStepHandler(operation))
    return registry
