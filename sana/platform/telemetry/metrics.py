"""Low-cardinality search performance, quality, health and cost metrics."""

from __future__ import annotations

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from sana.modules.orchestration.domain import AnswerQuality, BudgetUsage, SearchMode, StopReason
from sana.platform.telemetry.redaction import TelemetryRedactor


class SearchMetrics:
    def __init__(
        self,
        meter: Meter | None = None,
        *,
        redactor: TelemetryRedactor | None = None,
    ) -> None:
        self._meter = meter or metrics.get_meter("sana.search")
        self._redactor = redactor or TelemetryRedactor()
        self._run_latency = self._meter.create_histogram(
            "sana.search.run.duration",
            unit="s",
            description="End-to-end search run latency",
        )
        self._coverage = self._meter.create_histogram(
            "sana.search.fact.coverage",
            unit="1",
            description="Fraction of required facts covered",
        )
        self._upgrades = self._meter.create_counter(
            "sana.search.fast.upgrades",
            description="FAST to RESEARCH upgrades",
        )
        self._provider_latency = self._meter.create_histogram(
            "sana.search.provider.duration",
            unit="s",
        )
        self._provider_calls = self._meter.create_counter("sana.search.provider.calls")
        self._model_cost = self._meter.create_counter(
            "sana.search.model.cost",
            unit="USD",
        )
        self._retries = self._meter.create_counter("sana.search.step.retries")
        self._lease_expirations = self._meter.create_counter(
            "sana.search.step.lease_expirations"
        )
        self._usage = self._meter.create_histogram("sana.search.run.usage")

    def record_run(
        self,
        *,
        mode: SearchMode,
        latency_seconds: float,
        coverage_ratio: float,
        quality: AnswerQuality,
        stop_reason: StopReason,
        upgraded: bool,
        usage: BudgetUsage,
    ) -> None:
        if latency_seconds < 0 or not 0 <= coverage_ratio <= 1:
            raise ValueError("Run latency and coverage values are invalid")
        attributes = self._redactor.attributes(
            {
                "search.mode": mode.value,
                "search.answer_quality": quality.value,
                "search.stop_reason": stop_reason.value,
                "search.upgraded": upgraded,
            }
        )
        self._run_latency.record(latency_seconds, attributes)
        self._coverage.record(coverage_ratio, attributes)
        if upgraded:
            self._upgrades.add(1, attributes)
        for name, value in (
            ("queries", usage.query_count),
            ("providers", usage.provider_count),
            ("fetches", usage.fetch_count),
            ("llm_calls", usage.llm_call_count),
            ("expansion_rounds", usage.expansion_rounds),
        ):
            self._usage.record(value, {**attributes, "workflow.step.type": name})

    def record_provider(
        self,
        *,
        provider: str,
        status: str,
        latency_seconds: float,
    ) -> None:
        if latency_seconds < 0:
            raise ValueError("Provider latency cannot be negative")
        attributes = self._redactor.attributes(
            {"provider.name": provider, "provider.status": status}
        )
        self._provider_calls.add(1, attributes)
        self._provider_latency.record(latency_seconds, attributes)

    def record_model_cost(self, *, role: str, model: str, cost_usd: float) -> None:
        if cost_usd < 0:
            raise ValueError("Model cost cannot be negative")
        attributes = self._redactor.attributes(
            {"model.role": role, "model.name": model}
        )
        self._model_cost.add(cost_usd, attributes)

    def record_retry(self, *, step_type: str, count: int = 1) -> None:
        if count < 1:
            raise ValueError("Retry count must be positive")
        attributes = self._redactor.attributes({"workflow.step.type": step_type})
        self._retries.add(count, attributes)

    def record_lease_expiration(self, *, step_type: str) -> None:
        attributes = self._redactor.attributes({"workflow.step.type": step_type})
        self._lease_expirations.add(1, attributes)
