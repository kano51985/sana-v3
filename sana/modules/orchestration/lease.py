"""Deterministic lease creation and renewal policy."""

from __future__ import annotations

from datetime import datetime, timedelta

from sana.modules.orchestration.domain import SearchStep, StepAttempt, StepStatus
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import IdFactory


class LeaseService:
    def __init__(self, id_factory: IdFactory, lease_seconds: float = 30.0) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._ids = id_factory
        self._lease_seconds = lease_seconds

    def claim(
        self,
        step: SearchStep,
        *,
        attempt_no: int,
        worker_id: str,
        now: datetime,
        deadline_at: datetime,
    ) -> StepAttempt:
        if step.status is not StepStatus.READY:
            raise InvariantViolation("Only a READY step can be leased")
        if deadline_at <= now:
            raise InvariantViolation("Cannot lease a step past its deadline")
        leased_until = min(
            deadline_at,
            now + timedelta(seconds=self._lease_seconds),
        )
        step.start()
        return StepAttempt(
            id=self._ids.new_uuid(),
            tenant_id=step.tenant_id,
            run_id=step.run_id,
            step_id=step.id,
            attempt_no=attempt_no,
            idempotency_key=f"{step.id}:{attempt_no}",
            lease_owner=worker_id,
            leased_until=leased_until,
            deadline_at=deadline_at,
            started_at=now,
            input_ref=step.input_ref,
        )

    def recover_expired(
        self,
        step: SearchStep,
        attempt: StepAttempt,
        now: datetime,
    ) -> bool:
        if step.id != attempt.step_id:
            raise InvariantViolation("Attempt belongs to a different step")
        if step.status is StepStatus.RUNNING and attempt.lease_expired(now):
            step.release_expired_lease()
            return True
        return False
