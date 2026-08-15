from uuid import UUID

import pytest

from sana.modules.shadow_campaign.domain import ReviewActor, ReviewVerdict
from sana.modules.shadow_campaign.review import ReviewScore, ReviewSubmission


TENANT = UUID("10000000-0000-0000-0000-000000000001")
CAMPAIGN = UUID("20000000-0000-0000-0000-000000000001")
RESULT = UUID("30000000-0000-0000-0000-000000000001")
REVIEWER = UUID("40000000-0000-0000-0000-000000000001")


def test_human_review_is_canonical_and_requires_stable_reasons() -> None:
    review = ReviewSubmission(
        TENANT,
        CAMPAIGN,
        RESULT,
        "rubric-v1",
        ReviewVerdict.MINOR_ERROR,
        ReviewScore.PASS,
        ReviewScore.PASS,
        ReviewScore.PASS,
        ReviewScore.FAIL,
        ("missing_detail", "missing_detail"),
        ReviewActor.HUMAN,
        REVIEWER,
    )

    assert review.reason_codes == ("missing_detail",)
    assert len(review.sha256) == 64


def test_system_review_reasons_and_scores_are_closed() -> None:
    missing = ReviewSubmission.expected_answer_missing(
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        result_id=RESULT,
        rubric_version="rubric-v1",
    )
    unavailable = ReviewSubmission.material_unavailable(
        tenant_id=TENANT,
        campaign_id=CAMPAIGN,
        result_id=RESULT,
        rubric_version="rubric-v1",
    )

    assert missing.correctness_verdict is ReviewVerdict.MAJOR_ERROR
    assert missing.completeness is ReviewScore.FAIL
    assert unavailable.correctness_verdict is ReviewVerdict.UNREVIEWABLE
    assert unavailable.actor_type is ReviewActor.SYSTEM

    with pytest.raises(ValueError, match="allowlisted"):
        ReviewSubmission(
            TENANT,
            CAMPAIGN,
            RESULT,
            "rubric-v1",
            ReviewVerdict.MAJOR_ERROR,
            ReviewScore.FAIL,
            ReviewScore.FAIL,
            ReviewScore.FAIL,
            ReviewScore.FAIL,
            ("arbitrary_system_reason",),
            ReviewActor.SYSTEM,
            None,
        )

    with pytest.raises(ValueError, match="canonical"):
        ReviewSubmission(
            TENANT,
            CAMPAIGN,
            RESULT,
            "rubric-v1",
            ReviewVerdict.CORRECT,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ("expected_answer_missing",),
            ReviewActor.SYSTEM,
            None,
        )


def test_unreviewable_human_submission_cannot_smuggle_scores_or_free_text() -> None:
    with pytest.raises(ValueError, match="NOT_APPLICABLE"):
        ReviewSubmission(
            TENANT,
            CAMPAIGN,
            RESULT,
            "rubric-v1",
            ReviewVerdict.UNREVIEWABLE,
            ReviewScore.PASS,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ReviewScore.NOT_APPLICABLE,
            ("material_missing",),
            ReviewActor.HUMAN,
            REVIEWER,
        )

    with pytest.raises(ValueError, match="stable identifiers"):
        ReviewSubmission(
            TENANT,
            CAMPAIGN,
            RESULT,
            "rubric-v1",
            ReviewVerdict.MINOR_ERROR,
            ReviewScore.PASS,
            ReviewScore.PASS,
            ReviewScore.PASS,
            ReviewScore.FAIL,
            ("contains raw reviewer comment",),
            ReviewActor.HUMAN,
            REVIEWER,
        )

    with pytest.raises(ValueError, match="rubric"):
        ReviewSubmission(
            TENANT,
            CAMPAIGN,
            RESULT,
            "rubric-v1",
            ReviewVerdict.MINOR_ERROR,
            ReviewScore.PASS,
            ReviewScore.PASS,
            ReviewScore.PASS,
            ReviewScore.FAIL,
            ("secret_but_structurally_valid",),
            ReviewActor.HUMAN,
            REVIEWER,
        )
