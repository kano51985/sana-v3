from datetime import datetime, timedelta, timezone

import pytest

from sana.modules.content.domain import (
    DocumentReusePolicy,
    ReuseDecision,
    ReuseFreshness,
    ReuseWindow,
)
from sana.modules.shared.errors import ErrorCategory, TypedError


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)


def test_default_policy_uses_versioned_expected_windows() -> None:
    policy = DocumentReusePolicy.default()

    assert policy.version == "document-reuse-v1"
    assert policy.window_for(ReuseFreshness.STABLE) == ReuseWindow(
        timedelta(hours=24), timedelta(days=30)
    )
    assert policy.window_for(ReuseFreshness.RECENT) == ReuseWindow(
        timedelta(hours=6), timedelta(days=7)
    )
    assert policy.window_for(ReuseFreshness.CURRENT) == ReuseWindow(
        timedelta(minutes=15), timedelta(hours=2)
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((ReuseFreshness.STABLE,), ReuseFreshness.STABLE),
        ((ReuseFreshness.RECENT,), ReuseFreshness.RECENT),
        ((ReuseFreshness.CURRENT,), ReuseFreshness.CURRENT),
        (
            (ReuseFreshness.STABLE, ReuseFreshness.RECENT),
            ReuseFreshness.RECENT,
        ),
        (
            (ReuseFreshness.STABLE, ReuseFreshness.CURRENT),
            ReuseFreshness.CURRENT,
        ),
        (
            (ReuseFreshness.RECENT, ReuseFreshness.CURRENT),
            ReuseFreshness.CURRENT,
        ),
        (
            (
                ReuseFreshness.STABLE,
                ReuseFreshness.RECENT,
                ReuseFreshness.CURRENT,
            ),
            ReuseFreshness.CURRENT,
        ),
    ],
)
def test_strictest_freshness_is_order_independent(
    values: tuple[ReuseFreshness, ...], expected: ReuseFreshness
) -> None:
    policy = DocumentReusePolicy.default()

    assert policy.strictest(values) is expected
    assert policy.strictest(tuple(reversed(values))) is expected


def test_strictest_freshness_rejects_an_empty_mapping() -> None:
    with pytest.raises(ValueError, match="at least one"):
        DocumentReusePolicy.default().strictest(())


@pytest.mark.parametrize("freshness", tuple(ReuseFreshness))
def test_assessment_includes_closed_fresh_and_fallback_boundaries(
    freshness: ReuseFreshness,
) -> None:
    policy = DocumentReusePolicy.default()
    window = policy.window_for(freshness)

    fresh = policy.assess(freshness, NOW - window.fresh_for, NOW)
    stale = policy.assess(
        freshness,
        NOW - window.fresh_for - timedelta(microseconds=1),
        NOW,
    )
    expired = policy.assess(
        freshness,
        NOW - window.fallback_for - timedelta(microseconds=1),
        NOW,
    )

    assert fresh.decision is ReuseDecision.CACHE_FRESH
    assert fresh.fallback_eligible is True
    assert stale.decision is ReuseDecision.MISS
    assert stale.fallback_eligible is True
    assert expired.decision is ReuseDecision.MISS
    assert expired.fallback_eligible is False


def test_assessment_rejects_naive_or_future_timestamps() -> None:
    policy = DocumentReusePolicy.default()

    with pytest.raises(ValueError, match="timezone-aware"):
        policy.assess(ReuseFreshness.STABLE, NOW.replace(tzinfo=None), NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.assess(ReuseFreshness.STABLE, NOW, NOW.replace(tzinfo=None))
    with pytest.raises(TypedError) as raised:
        policy.assess(
            ReuseFreshness.STABLE,
            NOW + timedelta(microseconds=1),
            NOW,
        )
    assert raised.value.code == "cache_timestamp_invalid"
    assert raised.value.category is ErrorCategory.CONTENT


@pytest.mark.parametrize(
    "error",
    [
        TypedError(ErrorCategory.TRANSIENT, "fetch_network_failure", "network"),
        TypedError(ErrorCategory.TRANSIENT, "dns_resolution_failed", "dns"),
        TypedError(ErrorCategory.TRANSIENT, "dns_resolution_empty", "dns"),
        TypedError(
            ErrorCategory.BUDGET,
            "fetch_deadline_exceeded",
            "deadline",
            retryable=False,
        ),
        TypedError(ErrorCategory.TRANSIENT, "fetch_http_429", "rate"),
        TypedError(ErrorCategory.TRANSIENT, "fetch_http_500", "server"),
        TypedError(ErrorCategory.TRANSIENT, "fetch_http_599", "server"),
    ],
)
def test_stale_if_error_accepts_only_reviewed_transient_codes(
    error: TypedError,
) -> None:
    assert DocumentReusePolicy.default().allows_stale_if_error(error) is True


@pytest.mark.parametrize(
    "error",
    [
        TypedError(ErrorCategory.PERMANENT, "ssrf_blocked", "blocked"),
        TypedError(ErrorCategory.CONTENT, "fetch_http_404", "missing"),
        TypedError(ErrorCategory.CONTENT, "unsupported_content_type", "media"),
        TypedError(ErrorCategory.CONTENT, "response_too_large", "large"),
        TypedError(ErrorCategory.CONTENT, "empty_response_body", "empty"),
        TypedError(ErrorCategory.CONTENT, "artifact_corrupted", "digest"),
        TypedError(ErrorCategory.TRANSIENT, "unknown_transient", "unknown"),
        TypedError(ErrorCategory.INTERNAL, "unexpected_failure", "internal"),
    ],
)
def test_stale_if_error_rejects_unreviewed_failures(error: TypedError) -> None:
    assert DocumentReusePolicy.default().allows_stale_if_error(error) is False


@pytest.mark.parametrize(
    ("fresh_for", "fallback_for"),
    [
        (timedelta(0), timedelta(days=1)),
        (timedelta(hours=1), timedelta(0)),
        (timedelta(days=2), timedelta(days=1)),
    ],
)
def test_reuse_window_rejects_invalid_values(
    fresh_for: timedelta,
    fallback_for: timedelta,
) -> None:
    with pytest.raises(ValueError, match="reuse window"):
        ReuseWindow(fresh_for, fallback_for)
