"""Persistence ports owned by the orchestration domain."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sana.modules.orchestration.domain import SearchRun, SearchStep, StepAttempt


class RunRepository(Protocol):
    async def get(self, tenant_id: UUID, run_id: UUID) -> SearchRun | None: ...

    async def add(self, run: SearchRun) -> None: ...

    async def save(self, run: SearchRun) -> None: ...


class StepRepository(Protocol):
    async def get(self, tenant_id: UUID, step_id: UUID) -> SearchStep | None: ...

    async def get_for_update(
        self,
        tenant_id: UUID,
        step_id: UUID,
    ) -> SearchStep | None: ...

    async def add(self, step: SearchStep) -> None: ...

    async def save(self, step: SearchStep) -> None: ...


class AttemptRepository(Protocol):
    async def add(self, attempt: StepAttempt) -> None: ...

    async def next_attempt_no(self, tenant_id: UUID, step_id: UUID) -> int: ...

    async def complete(self, attempt: StepAttempt) -> None: ...

    async def renew(self, attempt: StepAttempt) -> None: ...


class UnitOfWork(Protocol):
    runs: RunRepository
    steps: StepRepository
    attempts: AttemptRepository

    async def __aenter__(self) -> "UnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
