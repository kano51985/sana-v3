"""Injectable clocks keep domain decisions deterministic in tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return a timezone-aware UTC-compatible timestamp."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(slots=True)
class FrozenClock:
    current: datetime

    def __post_init__(self) -> None:
        if self.current.tzinfo is None or self.current.utcoffset() is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> datetime:
        if delta.total_seconds() < 0:
            raise ValueError("Clock cannot move backwards")
        self.current += delta
        return self.current
