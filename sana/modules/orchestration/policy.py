"""Versioned time and resource limits for search workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from sana.modules.orchestration.domain import (
    BudgetSnapshot,
    BudgetUsage,
    SearchMode,
)
from sana.modules.shared.errors import ErrorCategory, TypedError


class BudgetPhase(StrEnum):
    ROUTE_PLAN = "route_plan"
    DISCOVERY = "discovery"
    FETCH_EXTRACT = "fetch_extract"
    VERIFY = "verify"
    SYNTHESIZE = "synthesize"


class BudgetExceeded(TypedError):
    def __init__(self, limit: str, used: float | int, maximum: float | int) -> None:
        super().__init__(
            ErrorCategory.BUDGET,
            "budget_exceeded",
            f"Budget limit exceeded: {limit}",
            retryable=False,
            details={"limit": limit, "used": used, "maximum": maximum},
        )


@dataclass(frozen=True, slots=True)
class ModePolicy:
    soft_seconds: float
    hard_seconds: float
    synthesis_reserve_seconds: float
    max_queries: int
    max_providers: int
    max_fetches: int
    max_llm_calls: int
    max_expansion_rounds: int
    phase_seconds: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "phase_seconds",
            MappingProxyType(dict(self.phase_seconds)),
        )


@dataclass(frozen=True, slots=True)
class SearchPolicy:
    version: str
    fast: ModePolicy
    research: ModePolicy

    @classmethod
    def default(cls) -> "SearchPolicy":
        return cls(
            version="search-v6",
            fast=ModePolicy(
                soft_seconds=14.0,
                hard_seconds=15.0,
                synthesis_reserve_seconds=1.0,
                max_queries=4,
                max_providers=2,
                max_fetches=4,
                max_llm_calls=4,
                max_expansion_rounds=1,
                phase_seconds={
                    BudgetPhase.ROUTE_PLAN.value: 1.2,
                    BudgetPhase.DISCOVERY.value: 4.2,
                    BudgetPhase.FETCH_EXTRACT.value: 3.6,
                    BudgetPhase.VERIFY.value: 2.0,
                    BudgetPhase.SYNTHESIZE.value: 1.0,
                },
            ),
            research=ModePolicy(
                soft_seconds=120.0,
                hard_seconds=120.0,
                synthesis_reserve_seconds=8.0,
                max_queries=12,
                max_providers=4,
                max_fetches=12,
                max_llm_calls=8,
                max_expansion_rounds=2,
                phase_seconds={
                    BudgetPhase.ROUTE_PLAN.value: 4.0,
                    BudgetPhase.DISCOVERY.value: 28.0,
                    BudgetPhase.FETCH_EXTRACT.value: 52.0,
                    BudgetPhase.VERIFY.value: 20.0,
                    BudgetPhase.SYNTHESIZE.value: 8.0,
                },
            ),
        )

    def snapshot(self, mode: SearchMode, created_at: datetime) -> BudgetSnapshot:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        selected = self.fast if mode is SearchMode.FAST else self.research
        return BudgetSnapshot(
            policy_version=self.version,
            created_at=created_at,
            soft_deadline_at=created_at + timedelta(seconds=selected.soft_seconds),
            hard_deadline_at=created_at + timedelta(seconds=selected.hard_seconds),
            synthesis_reserve_seconds=selected.synthesis_reserve_seconds,
            max_queries=selected.max_queries,
            max_providers=selected.max_providers,
            max_fetches=selected.max_fetches,
            max_llm_calls=selected.max_llm_calls,
            max_expansion_rounds=selected.max_expansion_rounds,
            phase_seconds=selected.phase_seconds,
        )


class BudgetGuard:
    def __init__(self, snapshot: BudgetSnapshot) -> None:
        self.snapshot = snapshot

    @property
    def non_synthesis_deadline(self) -> datetime:
        return self.snapshot.soft_deadline_at - timedelta(
            seconds=self.snapshot.synthesis_reserve_seconds
        )

    def deadline_for(self, phase: BudgetPhase) -> datetime:
        if phase is BudgetPhase.SYNTHESIZE:
            return self.snapshot.hard_deadline_at
        return self.non_synthesis_deadline

    def can_start(
        self,
        phase: BudgetPhase,
        now: datetime,
        *,
        estimated_seconds: float = 0.0,
    ) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if estimated_seconds < 0:
            raise ValueError("estimated_seconds cannot be negative")
        return now + timedelta(seconds=estimated_seconds) < self.deadline_for(phase)

    def validate(self, usage: BudgetUsage) -> None:
        limits = {
            "queries": (usage.query_count, self.snapshot.max_queries),
            "providers": (usage.provider_count, self.snapshot.max_providers),
            "fetches": (usage.fetch_count, self.snapshot.max_fetches),
            "llm_calls": (usage.llm_call_count, self.snapshot.max_llm_calls),
            "expansion_rounds": (
                usage.expansion_rounds,
                self.snapshot.max_expansion_rounds,
            ),
        }
        for name, (used, maximum) in limits.items():
            if used > maximum:
                raise BudgetExceeded(name, used, maximum)
        for phase, used in usage.phase_seconds.items():
            maximum = self.snapshot.phase_seconds.get(phase)
            if maximum is not None and used > maximum:
                raise BudgetExceeded(f"phase:{phase}", used, maximum)
