from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.orchestration.domain import (
    ArtifactRef,
    SearchStep,
    StepAttempt,
    StepStatus,
    StepType,
)
from sana.modules.shared.errors import ErrorCategory, InvariantViolation, TypedError


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)
INPUT = ArtifactRef("artifact://input", "a" * 64)
OUTPUT = ArtifactRef("artifact://output", "b" * 64)


def make_step() -> SearchStep:
    return SearchStep(
        uuid4(),
        uuid4(),
        uuid4(),
        "fetch:source-1",
        StepType.FETCH,
        1,
        INPUT,
    )


def test_step_identity_is_stable_across_retry() -> None:
    step = make_step()
    identity = step.identity_key

    step.start()
    step.retry_later(NOW + timedelta(seconds=2))
    step.make_ready()
    step.start()
    step.succeed(OUTPUT)

    assert step.identity_key == identity
    assert step.status is StepStatus.SUCCEEDED
    assert step.output_ref == OUTPUT
    with pytest.raises(InvariantViolation):
        step.start()


def test_expired_running_step_can_be_released_for_recovery() -> None:
    step = make_step()
    step.start()
    step.release_expired_lease()

    assert step.status is StepStatus.READY


def test_step_status_and_output_have_no_public_assignment_path() -> None:
    step = make_step()

    with pytest.raises(AttributeError):
        step.status = StepStatus.SUCCEEDED
    with pytest.raises(AttributeError):
        step.output_ref = OUTPUT


def test_attempt_output_and_completion_are_immutable() -> None:
    attempt = StepAttempt(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        "run:step:1",
        "worker-1",
        NOW + timedelta(seconds=10),
        NOW + timedelta(seconds=15),
        NOW,
        INPUT,
    )
    attempt.succeed(OUTPUT, NOW + timedelta(seconds=1))

    assert attempt.is_complete
    with pytest.raises(InvariantViolation):
        attempt.fail(
            TypedError(ErrorCategory.TRANSIENT, "late", "too late"),
            NOW + timedelta(seconds=2),
        )


def test_attempt_reports_expired_lease_only_while_incomplete() -> None:
    attempt = StepAttempt(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        1,
        "run:step:1",
        "worker-1",
        NOW + timedelta(seconds=1),
        NOW + timedelta(seconds=10),
        NOW,
        INPUT,
    )

    assert attempt.lease_expired(NOW + timedelta(seconds=2))
    attempt.fail(
        TypedError(ErrorCategory.TRANSIENT, "timeout", "timed out"),
        NOW + timedelta(seconds=2),
    )
    assert not attempt.lease_expired(NOW + timedelta(seconds=3))
