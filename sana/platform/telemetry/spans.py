"""Stable search span vocabulary with privacy-safe attributes."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode, Tracer

from sana.modules.shared.errors import TypedError
from sana.platform.telemetry.redaction import TelemetryRedactor


SEARCH_RUN_SPAN = "search.run"
PHASE_SPANS = frozenset(
    {
        "search.route",
        "search.plan",
        "search.provider",
        "search.fetch",
        "search.verify",
        "search.synthesize",
        "model.call",
    }
)


class SearchTracer:
    def __init__(
        self,
        tracer: Tracer | None = None,
        *,
        redactor: TelemetryRedactor | None = None,
    ) -> None:
        self._tracer = tracer or trace.get_tracer("sana.search")
        self._redactor = redactor or TelemetryRedactor()

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        if name != SEARCH_RUN_SPAN and name not in PHASE_SPANS:
            raise ValueError(f"Unknown search span name: {name}")
        safe_attributes = self._redactor.attributes(attributes or {})
        with self._tracer.start_as_current_span(
            name,
            attributes=safe_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as error:
                self.record_error(span, error)
                raise

    def record_error(self, span: Span, error: BaseException) -> None:
        values: dict[str, Any] = {"error.type": type(error).__name__}
        if isinstance(error, TypedError):
            values["error.code"] = error.code
        for key, value in self._redactor.attributes(values).items():
            span.set_attribute(key, value)
        span.set_status(Status(StatusCode.ERROR))
