from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.orchestration.domain import ArtifactRef, SearchStep, StepType
from sana.modules.orchestration.lease import LeaseService
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import DeterministicIdFactory, TraceContext
from sana.modules.orchestration.outbox import trace_context_to_dict
from sana.platform.queue.celery_app import create_celery_app
from sana.platform.queue.dispatcher import CeleryStepDispatcher, SearchQueue


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


class FakeCelery:
    def __init__(self) -> None:
        self.calls = []

    def send_task(self, name, **kwargs) -> None:
        self.calls.append((name, kwargs))


def test_duplicate_dispatch_uses_the_same_task_id_and_minimal_payload() -> None:
    app = FakeCelery()
    dispatcher = CeleryStepDispatcher(app)
    step_id = uuid4()
    trace = trace_context_to_dict(
        TraceContext.create(DeterministicIdFactory("dispatch"))
    )

    first = dispatcher.dispatch(step_id, trace, SearchQueue.FAST)
    second = dispatcher.dispatch(step_id, trace, SearchQueue.FAST)

    assert first == second == f"step:{step_id}"
    assert [call[1]["task_id"] for call in app.calls] == [first, first]
    assert app.calls[0][1]["args"] == [str(step_id), trace]


def test_database_step_state_absorbs_duplicate_worker_delivery() -> None:
    step = SearchStep(
        uuid4(),
        uuid4(),
        uuid4(),
        "route",
        StepType.ROUTE,
        1,
        ArtifactRef("db://messages/1", "a" * 64),
    )
    leases = LeaseService(DeterministicIdFactory("lease"))
    first = leases.claim(
        step,
        attempt_no=1,
        worker_id="worker-1",
        now=NOW,
        deadline_at=NOW + timedelta(seconds=15),
    )

    with pytest.raises(InvariantViolation, match="READY"):
        leases.claim(
            step,
            attempt_no=2,
            worker_id="worker-2",
            now=NOW,
            deadline_at=NOW + timedelta(seconds=15),
        )
    assert first.step_id == step.id


def test_celery_is_configured_for_late_ack_and_single_prefetch() -> None:
    app = create_celery_app("redis://localhost:6379/15")

    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_ignore_result is True
