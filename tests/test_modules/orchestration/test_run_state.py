from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from hypothesis import given, strategies as st

from sana.modules.orchestration.domain import (
    AnswerQuality,
    RoutingDecision,
    RunStatus,
    SearchMode,
    SearchRun,
    StopReason,
)
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shared.errors import InvariantViolation


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def make_run() -> SearchRun:
    policy = SearchPolicy.default()
    routing = RoutingDecision(SearchMode.FAST, ("single_fact",), policy.version, 0.9)
    return SearchRun(uuid4(), uuid4(), routing, policy.snapshot(SearchMode.FAST, NOW))


def test_controlled_timeout_is_successful_partial_answer() -> None:
    run = make_run()
    run.start(NOW)
    run.succeed(AnswerQuality.PARTIAL, StopReason.TIME_BUDGET, NOW + timedelta(seconds=12))

    assert run.status is RunStatus.SUCCEEDED
    assert run.answer_quality is AnswerQuality.PARTIAL
    assert run.stop_reason is StopReason.TIME_BUDGET


def test_complete_answer_requires_facts_covered() -> None:
    run = make_run()
    run.start(NOW)

    with pytest.raises(InvariantViolation):
        run.succeed(
            AnswerQuality.COMPLETE,
            StopReason.PROVIDER_FAILURE,
            NOW + timedelta(seconds=1),
        )


@given(st.lists(st.sampled_from(["start", "wait", "resume", "cancel"]), max_size=20))
def test_random_transition_sequences_never_escape_terminal_state(actions: list[str]) -> None:
    run = make_run()
    became_terminal = False

    for action in actions:
        try:
            if action == "start":
                run.start(NOW)
            elif action == "wait":
                run.wait()
            elif action == "resume":
                run.resume()
            else:
                run.cancel(NOW)
                became_terminal = True
        except InvariantViolation:
            pass
        if became_terminal:
            assert run.status is RunStatus.CANCELLED


def test_terminal_run_rejects_usage_updates() -> None:
    run = make_run()
    run.cancel(NOW)

    with pytest.raises(InvariantViolation):
        run.record_usage(run.usage.add(queries=1))


def test_run_status_has_no_public_assignment_path() -> None:
    run = make_run()

    with pytest.raises(AttributeError):
        run.status = RunStatus.SUCCEEDED
