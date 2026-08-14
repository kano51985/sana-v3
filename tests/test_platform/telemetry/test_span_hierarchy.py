from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sana.modules.orchestration.domain import (
    AnswerQuality,
    BudgetUsage,
    SearchMode,
    StopReason,
)
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.telemetry.metrics import SearchMetrics
from sana.platform.telemetry.spans import SearchTracer


def test_search_span_tree_preserves_parentage_without_raw_inputs() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = SearchTracer(provider.get_tracer("test"))

    with tracer.span(
        "search.run",
        {
            "search.mode": "RESEARCH",
            "search.policy_version": "search-v1",
            "run.id_hash": "run-123",
            "user.message": "private request",
        },
    ):
        with tracer.span("search.plan", {"workflow.step.type": "PLAN"}):
            with tracer.span(
                "model.call",
                {
                    "model.role": "planner",
                    "model.name": "fixture-model",
                    "model.prompt": "private prompt",
                },
            ):
                pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["search.plan"].parent.span_id == spans["search.run"].context.span_id
    assert spans["model.call"].parent.span_id == spans["search.plan"].context.span_id
    all_attributes = repr([dict(span.attributes) for span in spans.values()])
    assert "private request" not in all_attributes
    assert "private prompt" not in all_attributes
    assert spans["search.run"].attributes["run.id_hash"].startswith("sha256:")
    provider.shutdown()


def test_span_errors_record_type_and_code_but_not_exception_message() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = SearchTracer(provider.get_tracer("test"))

    try:
        with tracer.span("search.fetch"):
            raise TypedError(
                ErrorCategory.TRANSIENT,
                "upstream_timeout",
                "secret URL and response must not be exported",
            )
    except TypedError:
        pass

    span = exporter.get_finished_spans()[0]
    assert span.attributes["error.type"] == "TypedError"
    assert span.attributes["error.code"] == "upstream_timeout"
    assert span.events == ()
    assert "secret URL" not in repr(span)
    provider.shutdown()


def test_quality_health_cost_retry_and_lease_metrics_are_emitted() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    search_metrics = SearchMetrics(provider.get_meter("test"))

    search_metrics.record_run(
        mode=SearchMode.FAST,
        latency_seconds=8.2,
        coverage_ratio=0.75,
        quality=AnswerQuality.PARTIAL,
        stop_reason=StopReason.INSUFFICIENT_EVIDENCE,
        upgraded=True,
        usage=BudgetUsage(query_count=2, provider_count=2, fetch_count=1),
    )
    search_metrics.record_provider(
        provider="bing_rss",
        status="SUCCEEDED",
        latency_seconds=0.4,
    )
    search_metrics.record_model_cost(
        role="synthesizer",
        model="fixture-model",
        cost_usd=0.012,
    )
    search_metrics.record_retry(step_type="FETCH")
    search_metrics.record_lease_expiration(step_type="VERIFY")

    data = reader.get_metrics_data()
    names = {
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    assert {
        "sana.search.run.duration",
        "sana.search.fact.coverage",
        "sana.search.fast.upgrades",
        "sana.search.provider.duration",
        "sana.search.provider.calls",
        "sana.search.model.cost",
        "sana.search.step.retries",
        "sana.search.step.lease_expirations",
        "sana.search.run.usage",
    } <= names
    provider.shutdown()
