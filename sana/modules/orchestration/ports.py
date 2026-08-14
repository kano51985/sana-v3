"""Persistence ports owned by the orchestration domain."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sana.modules.orchestration.domain import SearchRun, SearchStep, StepAttempt


class RunRepository(Protocol):
    def get(self, tenant_id: UUID, run_id: UUID) -> SearchRun | None: ...

    def add(self, run: SearchRun) -> None: ...

    def save(self, run: SearchRun) -> None: ...


class StepRepository(Protocol):
    def get(self, tenant_id: UUID, step_id: UUID) -> SearchStep | None: ...

    def add(self, step: SearchStep) -> None: ...

    def save(self, step: SearchStep) -> None: ...


class AttemptRepository(Protocol):
    def add(self, attempt: StepAttempt) -> None: ...

    def next_attempt_no(self, tenant_id: UUID, step_id: UUID) -> int: ...


class UnitOfWork(Protocol):
    runs: RunRepository
    steps: StepRepository
    attempts: AttemptRepository

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
