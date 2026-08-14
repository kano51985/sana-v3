from datetime import datetime, timedelta, timezone

import pytest

from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import DeterministicIdFactory, TraceContext


def test_deterministic_factory_produces_stable_unique_ids() -> None:
    left = DeterministicIdFactory("fixture")
    right = DeterministicIdFactory("fixture")

    left_values = [left.new_uuid(), left.new_uuid(), left.new_span_id()]
    right_values = [right.new_uuid(), right.new_uuid(), right.new_span_id()]

    assert left_values == right_values
    assert len(set(left_values)) == 3


def test_trace_context_keeps_trace_id_for_child_spans() -> None:
    factory = DeterministicIdFactory("trace")
    parent = TraceContext.create(factory)
    child = parent.child(factory)

    assert child.trace_id == parent.trace_id
    assert child.span_id != parent.span_id
    assert parent.traceparent() == f"00-{parent.trace_id}-{parent.span_id}-01"


def test_trace_context_rejects_zero_or_malformed_identifiers() -> None:
    with pytest.raises(ValueError):
        TraceContext("0" * 32, "1" * 16)
    with pytest.raises(ValueError):
        TraceContext("not-a-trace", "1" * 16)


def test_frozen_clock_is_aware_and_monotonic() -> None:
    start = datetime(2026, 8, 14, tzinfo=timezone.utc)
    clock = FrozenClock(start)

    assert clock.advance(timedelta(seconds=5)) == start + timedelta(seconds=5)
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 8, 14))
