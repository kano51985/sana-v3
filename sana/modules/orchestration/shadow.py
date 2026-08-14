"""Run a non-user-visible candidate pipeline and persist only safe metric deltas."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeVar

from sana.platform.telemetry.redaction import TelemetryRedactor


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    mode: str
    status: str
    latency_ms: int
    cost_usd: float
    covered_facts: int
    total_facts: int
    citation_traceability: float
    query_pollution_count: int

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or self.cost_usd < 0:
            raise ValueError("Shadow latency and cost cannot be negative")
        if not 0 <= self.covered_facts <= self.total_facts:
            raise ValueError("Shadow fact counts are invalid")
        if not 0 <= self.citation_traceability <= 1:
            raise ValueError("Shadow citation traceability is invalid")
        if self.query_pollution_count < 0:
            raise ValueError("Shadow pollution count cannot be negative")


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    baseline: ShadowOutcome
    candidate: ShadowOutcome | None
    error_type: str | None = None
    error_code: str | None = None

    def safe_payload(self, redactor: TelemetryRedactor) -> dict[str, Any]:
        baseline = asdict(self.baseline)
        candidate = asdict(self.candidate) if self.candidate else None
        payload = {
            "baseline": baseline,
            "candidate": candidate,
            "delta": (
                {
                    "latency_ms": candidate["latency_ms"] - baseline["latency_ms"],
                    "cost_usd": candidate["cost_usd"] - baseline["cost_usd"],
                    "covered": candidate["covered_facts"] - baseline["covered_facts"],
                }
                if candidate
                else None
            ),
            "error_type": self.error_type,
            "error_code": self.error_code,
        }
        return redactor.diagnostic_payload(payload)


class ShadowSink(Protocol):
    async def write(self, payload: dict[str, Any]) -> None: ...


class ShadowRunner:
    def __init__(
        self,
        sink: ShadowSink,
        *,
        redactor: TelemetryRedactor | None = None,
    ) -> None:
        self._sink = sink
        self._redactor = redactor or TelemetryRedactor()
        self._collectors: set[asyncio.Task[None]] = set()

    async def execute(
        self,
        primary_call: Callable[[], Awaitable[tuple[T, ShadowOutcome]]],
        shadow_call: Callable[[], Awaitable[ShadowOutcome]],
    ) -> T:
        shadow_task = asyncio.create_task(shadow_call())
        try:
            primary_result, baseline = await primary_call()
        except BaseException:
            shadow_task.cancel()
            await asyncio.gather(shadow_task, return_exceptions=True)
            raise
        collector = asyncio.create_task(self._collect(shadow_task, baseline))
        self._collectors.add(collector)
        collector.add_done_callback(self._collector_done)
        return primary_result

    def _collector_done(self, task: asyncio.Task[None]) -> None:
        self._collectors.discard(task)
        if not task.cancelled():
            task.exception()

    async def _collect(
        self,
        shadow_task: asyncio.Task[ShadowOutcome],
        baseline: ShadowOutcome,
    ) -> None:
        try:
            candidate = await shadow_task
            if not isinstance(candidate, ShadowOutcome):
                raise TypeError("Shadow pipeline returned an invalid outcome")
            comparison = ShadowComparison(baseline, candidate)
        except BaseException as error:
            comparison = ShadowComparison(
                baseline,
                None,
                error_type=type(error).__name__,
                error_code=getattr(error, "code", None),
            )
        await self._sink.write(comparison.safe_payload(self._redactor))

    async def drain(self) -> None:
        if self._collectors:
            await asyncio.gather(*tuple(self._collectors))
