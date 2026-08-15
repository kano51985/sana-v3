from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


PATH = Path(__file__).parents[2] / "scripts" / "run_live_search_evals.py"
SPEC = importlib.util.spec_from_file_location("run_live_search_evals", PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUNNER
SPEC.loader.exec_module(RUNNER)


def test_live_runner_refuses_without_confirmation_before_client_creation() -> None:
    called = False

    def factory(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network client must not be created")

    assert RUNNER.main([], client_factory=factory) == 2
    assert called is False


@pytest.mark.parametrize("value", ("0", "21", "999"))
def test_live_runner_rejects_more_than_twenty_runs(value: str) -> None:
    with pytest.raises(SystemExit):
        RUNNER.main(["--confirm-live", "--max-runs", value])


def test_live_fixtures_are_bounded_and_reports_never_require_prompt_echo() -> None:
    cases = RUNNER._load_jsonl(
        Path(__file__).parents[2] / "evals" / "live_search_cases.jsonl"
    )

    assert 1 <= len(cases) <= 20
    fields = set(RUNNER.LiveRunResult.__dataclass_fields__)
    assert "prompt" not in fields
    assert "message" not in fields
    assert "quote" not in fields
