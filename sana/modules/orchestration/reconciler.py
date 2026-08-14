"""Recover dispatchable work from authoritative PostgreSQL state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from sana.modules.orchestration.domain import StepStatus


class ReconcileAction(StrEnum):
    DISPATCH_READY = "DISPATCH_READY"
    RELEASE_RETRY = "RELEASE_RETRY"
    RELEASE_EXPIRED_LEASE = "RELEASE_EXPIRED_LEASE"


@dataclass(frozen=True, slots=True)
class ReconcileCandidate:
    tenant_id: UUID
    step_id: UUID
    status: StepStatus
    retry_at: datetime | None = None
    leased_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    candidate: ReconcileCandidate
    action: ReconcileAction


class ReconciliationStore(Protocol):
    async def candidates(self, now: datetime, limit: int) -> list[ReconcileCandidate]: ...

    async def make_ready(self, tenant_id: UUID, step_id: UUID) -> None: ...

    async def enqueue(self, tenant_id: UUID, step_id: UUID) -> None: ...


class WorkflowReconciler:
    def decide(
        self,
        candidate: ReconcileCandidate,
        now: datetime,
    ) -> ReconcileDecision | None:
        if candidate.status is StepStatus.READY:
            return ReconcileDecision(candidate, ReconcileAction.DISPATCH_READY)
        if (
            candidate.status is StepStatus.RETRY_WAIT
            and candidate.retry_at is not None
            and candidate.retry_at <= now
        ):
            return ReconcileDecision(candidate, ReconcileAction.RELEASE_RETRY)
        if (
            candidate.status is StepStatus.RUNNING
            and candidate.leased_until is not None
            and candidate.leased_until <= now
        ):
            return ReconcileDecision(candidate, ReconcileAction.RELEASE_EXPIRED_LEASE)
        return None

    async def run(
        self,
        store: ReconciliationStore,
        now: datetime,
        *,
        limit: int = 100,
    ) -> list[ReconcileDecision]:
        decisions = [
            decision
            for candidate in await store.candidates(now, limit)
            if (decision := self.decide(candidate, now)) is not None
        ]
        for decision in decisions:
            candidate = decision.candidate
            if decision.action is not ReconcileAction.DISPATCH_READY:
                await store.make_ready(candidate.tenant_id, candidate.step_id)
            await store.enqueue(candidate.tenant_id, candidate.step_id)
        return decisions
