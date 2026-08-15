"""Owner-authorized application service for immutable Campaign reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.domain import ReviewActor
from sana.modules.shadow_campaign.review import (
    ReviewProjection,
    ReviewReceipt,
    ReviewSubmission,
)
from sana.modules.shared.errors import InvariantViolation

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import (
        CampaignReviewProjectionReader,
        CampaignUnitOfWorkFactory,
    )


class ShadowReviewService:
    def __init__(
        self,
        uow_factory: "CampaignUnitOfWorkFactory",
        projection_reader: "CampaignReviewProjectionReader",
    ) -> None:
        self._uow_factory = uow_factory
        self._projection_reader = projection_reader

    async def projection(
        self,
        principal: Principal,
        campaign_id: UUID,
        result_id: UUID,
    ) -> ReviewProjection | None:
        return await self._projection_reader.read(
            principal.tenant_id,
            principal.user_id,
            campaign_id,
            result_id,
        )

    async def submit_human(
        self,
        principal: Principal,
        submission: ReviewSubmission,
    ) -> ReviewReceipt:
        if (
            submission.actor_type is not ReviewActor.HUMAN
            or submission.tenant_id != principal.tenant_id
            or submission.reviewer_user_id != principal.user_id
        ):
            raise InvariantViolation(
                "Human review is not bound to the authenticated principal",
                code="review_principal_mismatch",
            )
        return await self._persist(submission)

    async def record_system(
        self,
        submission: ReviewSubmission,
    ) -> ReviewReceipt:
        if submission.actor_type is not ReviewActor.SYSTEM:
            raise ValueError("record_system requires a SYSTEM review")
        return await self._persist(submission)

    async def _persist(self, submission: ReviewSubmission) -> ReviewReceipt:
        async with self._uow_factory(submission.tenant_id) as uow:
            receipt = await uow.campaign_reviews.add(submission)
            await uow.commit()
            return receipt


__all__ = ["ShadowReviewService"]
