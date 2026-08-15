from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sana.app.workflow_completion import WorkflowCompletionCoordinator
from sana.modules.orchestration.domain import (
    ArtifactRef,
    RoutingDecision,
    SearchMode,
    SearchRun,
    SearchStep,
    StepType,
)
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import DeterministicIdFactory, TraceContext


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class RecordingArtifacts:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def put_json(self, tenant_id, run_id, payload):
        del tenant_id, run_id
        self.payloads.append(payload)
        return ArtifactRef(f"artifact://{len(self.payloads)}", f"{len(self.payloads):064x}")


class RecordingCoordinator(WorkflowCompletionCoordinator):
    def __init__(self, artifacts: RecordingArtifacts, plan_ref: ArtifactRef) -> None:
        super().__init__(
            artifacts,
            FrozenClock(NOW),
            DeterministicIdFactory("workflow-completion"),
        )
        self.rows = []
        self.plan_ref = plan_ref
        self.added: list[dict] = []

    async def _step_rows(self, uow, run):
        del uow, run
        return self.rows

    async def _plan_reference(self, uow, run, current=None):
        del uow, run, current
        return self.plan_ref

    async def _add_step(self, uow, run, **kwargs):
        del uow, run
        self.added.append(kwargs)
        return True


def _run() -> SearchRun:
    tenant_id = uuid4()
    return SearchRun(
        uuid4(),
        tenant_id,
        uuid4(),
        uuid4(),
        uuid4(),
        RoutingDecision(SearchMode.FAST, ("test",), "search-v1", 1.0),
        SearchPolicy.default().snapshot(SearchMode.FAST, NOW),
    )


def _successful_step(run: SearchRun, key: str, step_type: StepType, ref: ArtifactRef):
    step = SearchStep(
        uuid4(),
        run.tenant_id,
        run.id,
        key,
        step_type,
        1,
        ArtifactRef(f"input://{key}", "a" * 64),
    )
    step.start()
    step.succeed(ref)
    return step


@pytest.mark.asyncio
async def test_verify_fan_in_must_finish_before_synthesis_is_scheduled() -> None:
    run = _run()
    plan_ref = ArtifactRef("artifact://plan", "1" * 64)
    extract_ref = ArtifactRef("artifact://extract", "2" * 64)
    verify_ref = ArtifactRef("artifact://verify", "3" * 64)
    artifacts = RecordingArtifacts()
    coordinator = RecordingCoordinator(artifacts, plan_ref)
    extract = _successful_step(run, "extract:1", StepType.EXTRACT, extract_ref)
    coordinator.rows = [
        SimpleNamespace(
            id=uuid4(),
            step_type=StepType.FETCH.value,
            status="SUCCEEDED",
            output_ref={"uri": "artifact://fetch", "sha256": "4" * 64},
        ),
        SimpleNamespace(
            id=extract.id,
            step_type=StepType.EXTRACT.value,
            status="RUNNING",
            output_ref=None,
        ),
    ]

    await coordinator._maybe_verify(object(), run, extract, TraceContext.create())

    assert [item["step_type"] for item in coordinator.added] == [StepType.VERIFY]
    assert artifacts.payloads[-1]["extract_refs"] == [
        {"uri": extract_ref.uri, "sha256": extract_ref.sha256}
    ]

    verify = _successful_step(run, "verify", StepType.VERIFY, verify_ref)
    coordinator.rows = [
        SimpleNamespace(
            id=uuid4(),
            step_type=StepType.FETCH.value,
            status="SUCCEEDED",
            output_ref={"uri": "artifact://fetch", "sha256": "4" * 64},
        ),
        SimpleNamespace(
            id=extract.id,
            step_type=StepType.EXTRACT.value,
            status="SUCCEEDED",
            output_ref={"uri": extract_ref.uri, "sha256": extract_ref.sha256},
        ),
        SimpleNamespace(
            id=verify.id,
            step_type=StepType.VERIFY.value,
            status="RUNNING",
            output_ref=None,
        ),
    ]

    await coordinator._maybe_synthesize(object(), run, verify, TraceContext.create())

    assert [item["step_type"] for item in coordinator.added] == [
        StepType.VERIFY,
        StepType.SYNTHESIZE,
    ]
    assert artifacts.payloads[-1]["verify_ref"] == {
        "uri": verify_ref.uri,
        "sha256": verify_ref.sha256,
    }


@pytest.mark.asyncio
async def test_failed_verify_is_reported_as_pipeline_degradation() -> None:
    run = _run()
    plan_ref = ArtifactRef("artifact://plan", "1" * 64)
    artifacts = RecordingArtifacts()
    coordinator = RecordingCoordinator(artifacts, plan_ref)
    verify = SearchStep(
        uuid4(),
        run.tenant_id,
        run.id,
        "verify",
        StepType.VERIFY,
        1,
        ArtifactRef("input://verify", "a" * 64),
    )
    verify.start()
    verify.fail()
    coordinator.rows = [
        SimpleNamespace(
            id=verify.id,
            step_type=StepType.VERIFY.value,
            status="RUNNING",
            output_ref=None,
        )
    ]

    await coordinator._maybe_synthesize(
        object(),
        run,
        verify,
        TraceContext.create(),
        degradation_codes=("phase_deadline_exceeded",),
    )

    assert artifacts.payloads[-1]["verify_ref"] is None
    assert artifacts.payloads[-1]["pipeline_degradation_codes"] == [
        "phase_deadline_exceeded",
        "verify_failed",
    ]
