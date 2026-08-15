"""Tenant-scoped review projection and immutable structured review persistence."""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ReviewActor,
    SchedulingState,
)
from sana.modules.shadow_campaign.review import (
    ReviewCitationProjection,
    ReviewClaimProjection,
    ReviewProjection,
    ReviewReceipt,
    ReviewSubmission,
)
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.search import (
    AnswerClaim,
    Citation,
    DocumentChunk,
    DocumentVersion,
    EvidenceCandidate,
    VerifiedEvidence,
)
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowManualReviewRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.models.conversation import Message, ResponseRun
from sana.platform.db.models.orchestration import SearchRunRecord


class SqlShadowReviewProjectionReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
    ) -> ReviewProjection | None:
        session = self._session_factory()
        try:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            binding = (
                await session.execute(
                    select(
                        ShadowCampaignRecord.created_by_user_id,
                        ShadowCampaignRecord.review_rubric_version,
                        ShadowRunResultRecord.conversation_id,
                        ShadowRunResultRecord.search_run_id,
                        ShadowRunResultRecord.case_id,
                        ShadowRunResultRecord.repetition,
                        ShadowRunResultRecord.manual_review_selected,
                        ShadowRunResultRecord.scheduling_state,
                    )
                    .join(
                        ShadowRunResultRecord,
                        (
                            ShadowRunResultRecord.tenant_id
                            == ShadowCampaignRecord.tenant_id
                        )
                        & (
                            ShadowRunResultRecord.campaign_id
                            == ShadowCampaignRecord.id
                        ),
                    )
                    .where(
                        ShadowCampaignRecord.tenant_id == tenant_id,
                        ShadowCampaignRecord.id == campaign_id,
                        ShadowRunResultRecord.id == result_id,
                    )
                )
            ).one_or_none()
            if binding is None or binding.created_by_user_id != user_id:
                return None
            if (
                not binding.manual_review_selected
                or binding.scheduling_state != SchedulingState.COLLECTED.value
                or binding.conversation_id is None
                or binding.search_run_id is None
            ):
                raise InvariantViolation(
                    "Review projection is not available for this Result",
                    code="review_result_not_eligible",
                )

            answer_text = await session.scalar(
                select(Message.content)
                .join(
                    ResponseRun,
                    (ResponseRun.tenant_id == Message.tenant_id)
                    & (ResponseRun.output_message_id == Message.id),
                )
                .join(
                    SearchRunRecord,
                    (SearchRunRecord.tenant_id == ResponseRun.tenant_id)
                    & (SearchRunRecord.response_run_id == ResponseRun.id),
                )
                .where(
                    SearchRunRecord.tenant_id == tenant_id,
                    SearchRunRecord.id == binding.search_run_id,
                    SearchRunRecord.conversation_id == binding.conversation_id,
                    ResponseRun.conversation_id == binding.conversation_id,
                    Message.conversation_id == binding.conversation_id,
                    Message.role == "ASSISTANT",
                )
            )
            if answer_text is None:
                raise InvariantViolation(
                    "Review answer material is unavailable",
                    code="review_projection_invalid",
                )

            claims = tuple(
                (
                    await session.scalars(
                        select(AnswerClaim).where(
                            AnswerClaim.tenant_id == tenant_id,
                            AnswerClaim.run_id == binding.search_run_id,
                        ).order_by(AnswerClaim.id)
                    )
                ).all()
            )
            claim_by_id = {item.id: item for item in claims}
            if len(claim_by_id) != len(claims):
                raise InvariantViolation(
                    "Review projection contains duplicate Claim IDs",
                    code="review_projection_invalid",
                )
            citations_by_claim: dict[UUID, list[ReviewCitationProjection]] = {
                item.id: [] for item in claims
            }
            rows = (
                await session.execute(
                    select(
                        Citation,
                        VerifiedEvidence,
                        EvidenceCandidate,
                        DocumentVersion,
                        DocumentChunk,
                    )
                    .outerjoin(
                        VerifiedEvidence,
                        (VerifiedEvidence.tenant_id == Citation.tenant_id)
                        & (VerifiedEvidence.run_id == Citation.run_id)
                        & (VerifiedEvidence.id == Citation.verified_evidence_id),
                    )
                    .outerjoin(
                        EvidenceCandidate,
                        (EvidenceCandidate.tenant_id == VerifiedEvidence.tenant_id)
                        & (EvidenceCandidate.run_id == VerifiedEvidence.run_id)
                        & (EvidenceCandidate.id == VerifiedEvidence.candidate_id),
                    )
                    .outerjoin(
                        DocumentVersion,
                        (DocumentVersion.tenant_id == Citation.tenant_id)
                        & (DocumentVersion.id == Citation.document_version_id)
                        & (
                            DocumentVersion.id
                            == EvidenceCandidate.document_version_id
                        ),
                    )
                    .outerjoin(
                        DocumentChunk,
                        (DocumentChunk.tenant_id == Citation.tenant_id)
                        & (DocumentChunk.id == Citation.document_chunk_id)
                        & (
                            DocumentChunk.id
                            == EvidenceCandidate.document_chunk_id
                        )
                        & (
                            DocumentChunk.document_version_id
                            == DocumentVersion.id
                        ),
                    )
                    .where(
                        Citation.tenant_id == tenant_id,
                        Citation.run_id == binding.search_run_id,
                    )
                    .order_by(Citation.answer_claim_id, Citation.ordinal)
                )
            ).all()
            for citation, verified, candidate, version, chunk in rows:
                claim = claim_by_id.get(citation.answer_claim_id)
                if any(item is None for item in (claim, verified, candidate, version, chunk)):
                    raise InvariantViolation(
                        "Review Citation chain is incomplete",
                        code="review_projection_invalid",
                    )
                assert claim is not None
                assert verified is not None
                assert candidate is not None
                assert version is not None
                assert chunk is not None
                relative_start = citation.start_offset - chunk.start_offset
                relative_end = citation.end_offset - chunk.start_offset
                exact_quote = (
                    citation.start_offset == candidate.start_offset
                    and citation.end_offset == candidate.end_offset
                    and citation.quote == candidate.quote
                    and hashlib.sha256(candidate.quote.encode("utf-8")).hexdigest()
                    == candidate.quote_hash
                    and citation.end_offset - citation.start_offset
                    == len(citation.quote)
                    and relative_start >= 0
                    and citation.end_offset <= chunk.end_offset
                    and chunk.text_content[relative_start:relative_end]
                    == citation.quote
                )
                if (
                    not exact_quote
                    or verified.verdict != "ACCEPTED"
                    or candidate.fact_requirement_id != claim.fact_requirement_id
                    or claim.claim_kind != "FACTUAL"
                ):
                    raise InvariantViolation(
                        "Review Citation chain failed exact lineage validation",
                        code="review_projection_invalid",
                    )
                citations_by_claim[claim.id].append(
                    ReviewCitationProjection(
                        citation.id,
                        claim.id,
                        verified.id,
                        candidate.fact_requirement_id,
                        version.id,
                        chunk.id,
                        citation.ordinal,
                        verified.verdict,
                        verified.confidence,
                        candidate.source_authority,
                        version.fetched_at,
                        citation.start_offset,
                        citation.end_offset,
                        citation.label,
                        citation.rendered_url,
                        citation.quote,
                    )
                )

            projection_claims: list[ReviewClaimProjection] = []
            for claim in claims:
                if claim.claim_kind is None or (
                    claim.claim_kind == "FACTUAL"
                    and claim.fact_requirement_id is None
                ):
                    raise InvariantViolation(
                        "Review Claim is missing measurable lineage",
                        code="review_projection_invalid",
                    )
                projection_claims.append(
                    ReviewClaimProjection(
                        claim.id,
                        claim.claim_kind,
                        claim.fact_requirement_id,
                        claim.support_status,
                        tuple(citations_by_claim[claim.id]),
                        claim.claim_text,
                    )
                )
            return ReviewProjection(
                tenant_id,
                campaign_id,
                result_id,
                binding.conversation_id,
                binding.search_run_id,
                binding.case_id,
                binding.repetition,
                binding.review_rubric_version,
                tuple(projection_claims),
                answer_text,
            )
        finally:
            await session.rollback()
            await session.close()


class SqlShadowReviewRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def add(self, submission: ReviewSubmission) -> ReviewReceipt:
        if submission.tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == submission.tenant_id,
                ShadowCampaignRecord.id == submission.campaign_id,
            )
            .with_for_update()
        )
        if campaign is None:
            raise InvariantViolation("Campaign is missing", code="campaign_not_found")
        result = await self._session.scalar(
            select(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == submission.tenant_id,
                ShadowRunResultRecord.campaign_id == submission.campaign_id,
                ShadowRunResultRecord.id == submission.result_id,
            )
            .with_for_update()
        )
        if result is None:
            raise InvariantViolation("Result is missing", code="campaign_result_not_found")
        if (
            not result.manual_review_selected
            or result.scheduling_state != SchedulingState.COLLECTED.value
        ):
            raise InvariantViolation(
                "Result is not an eligible preselected review unit",
                code="review_result_not_eligible",
            )
        if submission.rubric_version != campaign.review_rubric_version:
            raise InvariantViolation(
                "Review rubric does not match the frozen Campaign",
                code="review_rubric_mismatch",
            )
        if (
            submission.actor_type is ReviewActor.HUMAN
            and submission.reviewer_user_id != campaign.created_by_user_id
        ):
            raise InvariantViolation(
                "Only the Campaign owner may submit a human review",
                code="review_owner_mismatch",
            )
        if (
            submission.reason_codes == ("expected_answer_missing",)
            and result.answer_quality not in {None, "NONE"}
        ):
            raise InvariantViolation(
                "Expected-answer-missing review conflicts with the Result",
                code="system_review_evidence_mismatch",
            )

        existing = await self._session.scalar(
            select(ShadowManualReviewRecord).where(
                ShadowManualReviewRecord.tenant_id == submission.tenant_id,
                ShadowManualReviewRecord.result_id == submission.result_id,
                ShadowManualReviewRecord.rubric_version == submission.rubric_version,
            )
        )
        if existing is not None:
            if not self._matches(existing, submission):
                raise InvariantViolation(
                    "Review was already submitted with different values",
                    code="review_conflict",
                )
            return ReviewReceipt(result.id, existing.id, True)

        if (
            campaign.status != CampaignStatus.AWAITING_REVIEW.value
            or campaign.review_deadline_at is None
            or now > campaign.review_deadline_at
        ):
            raise InvariantViolation(
                "Campaign is outside its review window",
                code="review_window_closed",
            )

        review_id = uuid5(
            result.id,
            f"shadow-review:{submission.rubric_version}",
        )
        self._session.add(
            ShadowManualReviewRecord(
                id=review_id,
                tenant_id=submission.tenant_id,
                campaign_id=submission.campaign_id,
                result_id=submission.result_id,
                rubric_version=submission.rubric_version,
                correctness_verdict=submission.correctness_verdict.value,
                citation_relevance=submission.citation_relevance.value,
                source_appropriateness=submission.source_appropriateness.value,
                freshness=submission.freshness.value,
                completeness=submission.completeness.value,
                reason_codes=list(submission.reason_codes),
                actor_type=submission.actor_type.value,
                reviewer_user_id=submission.reviewer_user_id,
                reviewed_at=now,
                retention_until=result.retention_until,
            )
        )
        campaign.version += 1
        campaign.updated_at = now
        await self._session.flush()
        return ReviewReceipt(result.id, review_id, False)

    @staticmethod
    def _matches(
        record: ShadowManualReviewRecord,
        submission: ReviewSubmission,
    ) -> bool:
        return bool(
            record.campaign_id == submission.campaign_id
            and record.correctness_verdict == submission.correctness_verdict.value
            and record.citation_relevance == submission.citation_relevance.value
            and record.source_appropriateness
            == submission.source_appropriateness.value
            and record.freshness == submission.freshness.value
            and record.completeness == submission.completeness.value
            and tuple(record.reason_codes) == submission.reason_codes
            and record.actor_type == submission.actor_type.value
            and record.reviewer_user_id == submission.reviewer_user_id
        )


__all__ = ["SqlShadowReviewProjectionReader", "SqlShadowReviewRepository"]
