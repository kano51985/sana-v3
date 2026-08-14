"""Build isolated OpenTelemetry providers; global installation is explicit."""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from sana.platform.telemetry.metrics import SearchMetrics
from sana.platform.telemetry.redaction import TelemetryRedactor
from sana.platform.telemetry.spans import SearchTracer


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    enabled: bool = False
    service_name: str = "sana"
    environment: str = "development"
    otlp_endpoint: str | None = None
    identifier_hash_salt: str = "sana-telemetry-v1"

    def __post_init__(self) -> None:
        if not self.service_name.strip() or not self.environment.strip():
            raise ValueError("Telemetry service and environment cannot be empty")
        if self.enabled and not self.otlp_endpoint:
            raise ValueError("Enabled telemetry requires an OTLP endpoint")


@dataclass(slots=True)
class TelemetryRuntime:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    tracer: SearchTracer
    metrics: SearchMetrics

    def install_global(self) -> None:
        trace.set_tracer_provider(self.tracer_provider)
        metrics.set_meter_provider(self.meter_provider)

    def shutdown(self) -> None:
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()


def build_telemetry(
    config: TelemetryConfig,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> TelemetryRuntime:
    resource = Resource.create(
        {
            "service.name": config.service_name,
            "deployment.environment.name": config.environment,
        }
    )
    if config.enabled and span_exporter is None:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter(endpoint=config.otlp_endpoint)
    if config.enabled and metric_reader is None:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=config.otlp_endpoint)
        )
    tracer_provider = TracerProvider(resource=resource)
    if span_exporter is not None:
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=([metric_reader] if metric_reader is not None else []),
    )
    redactor = TelemetryRedactor(hash_salt=config.identifier_hash_salt)
    return TelemetryRuntime(
        tracer_provider,
        meter_provider,
        SearchTracer(tracer_provider.get_tracer("sana.search"), redactor=redactor),
        SearchMetrics(meter_provider.get_meter("sana.search"), redactor=redactor),
    )
