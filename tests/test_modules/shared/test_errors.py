import pytest

from sana.modules.shared.errors import ErrorCategory, InvariantViolation, TypedError
from sana.modules.shared.result import Result


def test_transient_error_defaults_to_retryable_and_serializes() -> None:
    error = TypedError(
        ErrorCategory.TRANSIENT,
        "provider_timeout",
        "Provider timed out",
        details={"provider": "example"},
    )

    assert error.retryable is True
    assert error.to_dict() == {
        "category": "TRANSIENT",
        "code": "provider_timeout",
        "message": "Provider timed out",
        "retryable": True,
        "details": {"provider": "example"},
    }
    with pytest.raises(TypeError):
        error.details["provider"] = "changed"


def test_invariant_violation_is_permanent() -> None:
    error = InvariantViolation("illegal transition")

    assert error.category is ErrorCategory.PERMANENT
    assert error.retryable is False
    assert "illegal transition" in str(error)


def test_result_preserves_typed_error_and_supports_none_success() -> None:
    success = Result.ok(None)
    failure = Result.err(InvariantViolation("broken"))

    assert success.is_ok
    assert success.unwrap() is None
    assert failure.is_err
    with pytest.raises(InvariantViolation):
        failure.unwrap()


def test_result_requires_exactly_one_branch() -> None:
    with pytest.raises(ValueError):
        Result()
    with pytest.raises(ValueError):
        Result(value="ok", error=InvariantViolation("broken"))
