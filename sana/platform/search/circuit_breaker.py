"""Small provider-local closed/open/half-open circuit breaker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sana.modules.shared.clock import Clock


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(slots=True)
class CircuitBreaker:
    clock: Clock
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    opened_at: datetime | None = None
    _probe_in_flight: bool = False

    def __post_init__(self) -> None:
        if self.failure_threshold < 1 or self.recovery_seconds <= 0:
            raise ValueError("Circuit breaker limits must be positive")

    def allow_request(self) -> bool:
        if self.state is CircuitState.CLOSED:
            return True
        if self.state is CircuitState.OPEN:
            assert self.opened_at is not None
            if self.clock.now() < self.opened_at + timedelta(seconds=self.recovery_seconds):
                return False
            self.state = CircuitState.HALF_OPEN
            self._probe_in_flight = False
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self.state is CircuitState.HALF_OPEN:
            self._open()
            return
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self._open()

    def _open(self) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = self.clock.now()
