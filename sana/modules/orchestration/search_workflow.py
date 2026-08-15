"""Bounded FAST workflow graph, reservations and terminal outcome policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from sana.modules.evidence.coverage import FactCoverage
from sana.modules.orchestration.domain import (
    AnswerQuality,
    BudgetUsage,
    SearchMode,
    SearchRun,
    StepStatus,
    StepType,
    StopReason,
)
from sana.modules.orchestration.policy import BudgetExceeded, BudgetGuard, BudgetPhase
from sana.modules.shared.clock import Clock


@dataclass(frozen=True, slots=True)
class StepBudgetCost:
    phase: BudgetPhase
    estimated_seconds: float = 0.0
    queries: int = 0
    providers: int = 0
    fetches: int = 0
    llm_calls: int = 0

    def __post_init__(self) -> None:
        if self.estimated_seconds < 0 or any(
            value < 0
            for value in (self.queries, self.providers, self.fetches, self.llm_calls)
        ):
            raise ValueError("Step budget cost cannot be negative")

    def apply(self, usage: BudgetUsage, *, elapsed_seconds: float | None = None) -> BudgetUsage:
        # Provider calls are reserved atomically by ModelInvocationAuditSink.
        # ``llm_calls`` remains readable for old graph snapshots but is never
        # applied here, preventing Step completion from counting it twice.
        return usage.add(
            queries=self.queries,
            providers=self.providers,
            fetches=self.fetches,
            llm_calls=0,
            phase=self.phase.value,
            elapsed_seconds=(
                self.estimated_seconds if elapsed_seconds is None else elapsed_seconds
            ),
        )


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    step_key: str
    cost: StepBudgetCost


class BudgetReservationLedger:
    """Reservations are reconstructible from pending durable Step specifications."""

    def __init__(
        self,
        guard: BudgetGuard,
        usage: BudgetUsage,
        *,
        pending: tuple[BudgetReservation, ...] = (),
    ) -> None:
        self._guard = guard
        self._usage = usage
        self._reservations: dict[str, StepBudgetCost] = {}
        for reservation in pending:
            if reservation.step_key in self._reservations:
                raise ValueError("Pending budget reservation keys must be unique")
            self._reservations[reservation.step_key] = reservation.cost
        self._guard.validate(self.projected_usage)

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    @property
    def reservations(self) -> tuple[BudgetReservation, ...]:
        return tuple(
            BudgetReservation(key, cost)
            for key, cost in self._reservations.items()
        )

    @property
    def projected_usage(self) -> BudgetUsage:
        projected = self._usage
        for cost in self._reservations.values():
            projected = cost.apply(projected)
        return projected

    def reserve(
        self,
        step_key: str,
        cost: StepBudgetCost,
        *,
        now: datetime,
    ) -> BudgetReservation:
        if not step_key.strip() or step_key in self._reservations:
            raise ValueError("Step already has a reservation or has an empty key")
        if not self._guard.can_start(
            cost.phase,
            now,
            estimated_seconds=cost.estimated_seconds,
        ):
            raise BudgetExceeded("deadline", cost.estimated_seconds, 0)
        self._guard.validate(cost.apply(self.projected_usage))
        self._reservations[step_key] = cost
        return BudgetReservation(step_key, cost)

    def complete(
        self,
        step_key: str,
        actual: StepBudgetCost,
        *,
        elapsed_seconds: float,
    ) -> BudgetUsage:
        reserved = self._reservations.get(step_key)
        if reserved is None:
            raise ValueError("Step has no budget reservation")
        if actual.phase is not reserved.phase:
            raise ValueError("Actual usage phase differs from reservation")
        self._reservations.pop(step_key)
        self._usage = actual.apply(self._usage, elapsed_seconds=elapsed_seconds)
        self._guard.validate(self._usage)
        return self._usage

    def release(self, step_key: str) -> None:
        if self._reservations.pop(step_key, None) is None:
            raise ValueError("Step has no budget reservation")

    def release_if_present(self, step_key: str) -> bool:
        return self._reservations.pop(step_key, None) is not None


@dataclass(frozen=True, slots=True)
class WorkflowStepSpec:
    key: str
    step_type: StepType
    dependencies: tuple[str, ...]
    budget: StepBudgetCost
    low_value_after_deadline: bool

    def __post_init__(self) -> None:
        if not self.key.strip() or any(not item.strip() for item in self.dependencies):
            raise ValueError("Workflow step keys cannot be empty")
        if self.key in self.dependencies:
            raise ValueError("Workflow step cannot depend on itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "step_type": self.step_type.value,
            "dependencies": list(self.dependencies),
            "budget": {
                "phase": self.budget.phase.value,
                "estimated_seconds": self.budget.estimated_seconds,
                "queries": self.budget.queries,
                "providers": self.budget.providers,
                "fetches": self.budget.fetches,
                "llm_calls": self.budget.llm_calls,
            },
            "low_value_after_deadline": self.low_value_after_deadline,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "WorkflowStepSpec":
        budget = dict(payload["budget"])
        return cls(
            key=str(payload["key"]),
            step_type=StepType(str(payload["step_type"])),
            dependencies=tuple(map(str, payload.get("dependencies", ()))),
            budget=StepBudgetCost(
                phase=BudgetPhase(str(budget["phase"])),
                estimated_seconds=float(budget.get("estimated_seconds", 0)),
                queries=int(budget.get("queries", 0)),
                providers=int(budget.get("providers", 0)),
                fetches=int(budget.get("fetches", 0)),
                llm_calls=int(budget.get("llm_calls", 0)),
            ),
            low_value_after_deadline=bool(payload["low_value_after_deadline"]),
        )


@dataclass(frozen=True, slots=True)
class WorkflowAdvance:
    submit: tuple[WorkflowStepSpec, ...]
    cancel: tuple[str, ...]
    skip: tuple[str, ...]
    release_reservations: tuple[str, ...]
    deadline_fallback: bool


@dataclass(frozen=True, slots=True)
class FastWorkflowOutcome:
    quality: AnswerQuality
    reason: StopReason
    request_research_upgrade: bool = False


class _GraphStage(StrEnum):
    OPEN = "OPEN"
    DISCOVERY_SEALED = "DISCOVERY_SEALED"
    SYNTHESIS_SEALED = "SYNTHESIS_SEALED"


class FastSearchGraph:
    """Materialize fan-out Steps; returned peers are dispatched by workers."""

    def __init__(self) -> None:
        self._nodes: dict[str, WorkflowStepSpec] = {}
        self._stage = _GraphStage.OPEN
        self._add(
            WorkflowStepSpec(
                "route",
                StepType.ROUTE,
                (),
                StepBudgetCost(BudgetPhase.ROUTE_PLAN, estimated_seconds=0.1),
                False,
            )
        )
        self._add(
            WorkflowStepSpec(
                "plan",
                StepType.PLAN,
                ("route",),
                StepBudgetCost(
                    BudgetPhase.ROUTE_PLAN,
                    estimated_seconds=0.8,
                ),
                False,
            )
        )

    @property
    def nodes(self) -> Mapping[str, WorkflowStepSpec]:
        return MappingProxyType(dict(self._nodes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "workflow": SearchMode.FAST.value,
            "stage": self._stage.value,
            "nodes": [node.to_dict() for node in self._nodes.values()],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FastSearchGraph":
        if payload.get("schema_version") != 1 or payload.get("workflow") != "FAST":
            raise ValueError("Unsupported FAST workflow graph snapshot")
        graph = cls.__new__(cls)
        graph._nodes = {}
        graph._stage = _GraphStage(str(payload["stage"]))
        nodes = payload.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError("Workflow graph snapshot has no nodes")
        for value in nodes:
            if not isinstance(value, Mapping):
                raise ValueError("Workflow graph node must be an object")
            graph._add(WorkflowStepSpec.from_dict(value))
        if set(("route", "plan")) - set(graph._nodes):
            raise ValueError("Workflow graph snapshot lacks required bootstrap steps")
        if graph._stage is not _GraphStage.OPEN and "select" not in graph._nodes:
            raise ValueError("Sealed workflow graph snapshot lacks select step")
        if (
            graph._stage is _GraphStage.SYNTHESIS_SEALED
            and "synthesize" not in graph._nodes
        ):
            raise ValueError("Sealed workflow graph snapshot lacks synthesis step")
        return graph

    def _add(self, node: WorkflowStepSpec) -> None:
        if node.key in self._nodes:
            raise ValueError(f"Duplicate workflow step key: {node.key}")
        missing = set(node.dependencies) - set(self._nodes)
        if missing:
            raise ValueError(f"Workflow step has unknown dependencies: {sorted(missing)}")
        self._nodes[node.key] = node

    def add_discovery(
        self,
        query_key: str,
        *,
        provider_slots: int = 1,
        estimated_seconds: float = 1.0,
    ) -> str:
        if self._stage is not _GraphStage.OPEN:
            raise ValueError("Discovery fan-out is already sealed")
        if not query_key.strip():
            raise ValueError("Discovery query key cannot be empty")
        key = f"discover:{query_key}"
        self._add(
            WorkflowStepSpec(
                key,
                StepType.DISCOVERY,
                ("plan",),
                StepBudgetCost(
                    BudgetPhase.DISCOVERY,
                    estimated_seconds=estimated_seconds,
                    queries=1,
                    providers=provider_slots,
                ),
                True,
            )
        )
        return key

    def seal_discovery(self) -> None:
        if self._stage is not _GraphStage.OPEN:
            raise ValueError("Discovery fan-out is already sealed")
        dependencies = tuple(
            key for key, node in self._nodes.items() if node.step_type is StepType.DISCOVERY
        ) or ("plan",)
        self._add(
            WorkflowStepSpec(
                "select",
                StepType.SELECT,
                dependencies,
                StepBudgetCost(BudgetPhase.DISCOVERY, estimated_seconds=0.2),
                True,
            )
        )
        self._stage = _GraphStage.DISCOVERY_SEALED

    def add_fetch_pipeline(
        self,
        source_key: str,
        *,
        fetch_seconds: float = 1.0,
        verify_seconds: float = 0.3,
    ) -> tuple[str, str]:
        del verify_seconds
        if self._stage is not _GraphStage.DISCOVERY_SEALED:
            raise ValueError("Fetch pipelines require sealed discovery")
        if not source_key.strip():
            raise ValueError("Fetch source key cannot be empty")
        fetch_key = f"fetch:{source_key}"
        extract_key = f"extract:{source_key}"
        self._add(
            WorkflowStepSpec(
                fetch_key,
                StepType.FETCH,
                ("select",),
                StepBudgetCost(
                    BudgetPhase.FETCH_EXTRACT,
                    estimated_seconds=fetch_seconds,
                    fetches=1,
                ),
                True,
            )
        )
        self._add(
            WorkflowStepSpec(
                extract_key,
                StepType.EXTRACT,
                (fetch_key,),
                StepBudgetCost(BudgetPhase.FETCH_EXTRACT, estimated_seconds=0.2),
                True,
            )
        )
        return fetch_key, extract_key

    def seal_synthesis(self) -> None:
        if self._stage is not _GraphStage.DISCOVERY_SEALED:
            raise ValueError("Synthesis requires sealed discovery")
        dependencies = tuple(
            key for key, node in self._nodes.items() if node.step_type is StepType.EXTRACT
        ) or ("select",)
        self._add(
            WorkflowStepSpec(
                "verify",
                StepType.VERIFY,
                dependencies,
                StepBudgetCost(BudgetPhase.VERIFY, estimated_seconds=0.3),
                True,
            )
        )
        self._add(
            WorkflowStepSpec(
                "synthesize",
                StepType.SYNTHESIZE,
                ("verify",),
                StepBudgetCost(
                    BudgetPhase.SYNTHESIZE,
                    estimated_seconds=0.8,
                ),
                False,
            )
        )
        self._stage = _GraphStage.SYNTHESIS_SEALED

    def advance(
        self,
        statuses: Mapping[str, StepStatus],
        *,
        guard: BudgetGuard,
        clock: Clock,
    ) -> WorkflowAdvance:
        if self._stage is not _GraphStage.SYNTHESIS_SEALED:
            raise ValueError("Workflow graph must be sealed before scheduling")
        unknown = set(statuses) - set(self._nodes)
        if unknown:
            raise ValueError(f"Statuses contain unknown steps: {sorted(unknown)}")
        now = clock.now()
        if now >= guard.non_synthesis_deadline:
            cancel = tuple(
                key
                for key, node in self._nodes.items()
                if node.low_value_after_deadline
                and statuses.get(key, StepStatus.READY)
                in {StepStatus.READY, StepStatus.RUNNING, StepStatus.RETRY_WAIT}
            )
            synthesize = self._nodes["synthesize"]
            submit = (
                (synthesize,)
                if statuses.get("synthesize", StepStatus.READY) is StepStatus.READY
                else ()
            )
            release_reservations = tuple(
                key
                for key in cancel
                if statuses.get(key, StepStatus.READY)
                in {StepStatus.READY, StepStatus.RETRY_WAIT}
            )
            return WorkflowAdvance(
                submit,
                cancel,
                (),
                release_reservations,
                True,
            )

        terminal = {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
            StepStatus.CANCELLED,
        }
        skip = tuple(
            key
            for key, node in self._nodes.items()
            if node.step_type in {StepType.EXTRACT, StepType.VERIFY}
            and statuses.get(key, StepStatus.READY) is StepStatus.READY
            and all(statuses.get(dependency) in terminal for dependency in node.dependencies)
            and (
                any(
                    statuses.get(dependency) is not StepStatus.SUCCEEDED
                    for dependency in node.dependencies
                )
                if node.step_type is StepType.EXTRACT
                else all(
                    statuses.get(dependency) is not StepStatus.SUCCEEDED
                    for dependency in node.dependencies
                )
            )
        )

        def dependencies_satisfied(node: WorkflowStepSpec) -> bool:
            dependency_statuses = tuple(
                statuses.get(dependency) for dependency in node.dependencies
            )
            if node.step_type in {StepType.SELECT, StepType.SYNTHESIZE} and all(
                self._nodes[dependency].step_type
                in {StepType.DISCOVERY, StepType.VERIFY}
                for dependency in node.dependencies
            ):
                return all(status in terminal for status in dependency_statuses)
            if node.step_type is StepType.VERIFY and all(
                self._nodes[dependency].step_type is StepType.EXTRACT
                for dependency in node.dependencies
            ):
                return all(status in terminal for status in dependency_statuses) and any(
                    status is StepStatus.SUCCEEDED for status in dependency_statuses
                )
            return all(status is StepStatus.SUCCEEDED for status in dependency_statuses)

        ready = tuple(
            node
            for key, node in self._nodes.items()
            if statuses.get(key, StepStatus.READY) is StepStatus.READY
            and key not in skip
            and dependencies_satisfied(node)
        )
        return WorkflowAdvance(ready, (), skip, (), False)


class FastSearchWorkflow:
    def __init__(self, graph: FastSearchGraph) -> None:
        self.graph = graph

    def reserve_submissions(
        self,
        advance: WorkflowAdvance,
        ledger: BudgetReservationLedger,
        *,
        clock: Clock,
    ) -> tuple[WorkflowStepSpec, ...]:
        for step_key in advance.release_reservations:
            ledger.release_if_present(step_key)
        reserved: list[WorkflowStepSpec] = []
        try:
            for node in advance.submit:
                ledger.reserve(node.key, node.budget, now=clock.now())
                reserved.append(node)
        except Exception:
            for node in reserved:
                ledger.release_if_present(node.key)
            raise
        return tuple(reserved)

    @staticmethod
    def outcome(
        required_fact_statuses: tuple[FactCoverage, ...],
        *,
        deadline_fallback: bool,
    ) -> FastWorkflowOutcome:
        complete = bool(required_fact_statuses) and all(
            status in {FactCoverage.COVERED, FactCoverage.VERIFIED}
            for status in required_fact_statuses
        )
        if complete:
            return FastWorkflowOutcome(AnswerQuality.COMPLETE, StopReason.FACTS_COVERED)
        return FastWorkflowOutcome(
            AnswerQuality.PARTIAL,
            (
                StopReason.TIME_BUDGET
                if deadline_fallback
                else StopReason.INSUFFICIENT_EVIDENCE
            ),
            False,
        )

    @staticmethod
    def finish(run: SearchRun, outcome: FastWorkflowOutcome, *, clock: Clock) -> None:
        if run.mode is not SearchMode.FAST:
            raise ValueError("Fast workflow cannot finish a RESEARCH run")
        run.succeed(outcome.quality, outcome.reason, clock.now())
