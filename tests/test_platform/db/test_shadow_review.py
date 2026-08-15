from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text, update

from sana.app.shadow_review import ShadowReviewService
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import SearchMode
from sana.modules.shadow_campaign.domain import ReviewActor, ReviewVerdict, snapshot_hash
from sana.modules.shadow_campaign.manifest import (
    Answerability,
    CaseCategory,
    GoldAssertion,
    OracleType,
    ShadowCase,
    ShadowManifest,
)
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CostRate,
    DOCKER_SMOKE_V1,
    ReviewRubric,
)
from sana.modules.shadow_campaign.review import ReviewScore, ReviewSubmission
from sana.modules.shadow_campaign.scheduler import CampaignSchedulingService
from sana.modules.shadow_campaign.service import (
    CampaignLifecycleService,
    CampaignProvenance,
    CampaignService,
    CreateCampaignCommand,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import DeterministicIdFactory
from sana.platform.db.models.conversation import Conversation, Message, ResponseRun
from sana.platform.db.models.orchestration import SearchRunRecord
from sana.platform.db.models.search import (
    AnswerClaim,
    Citation,
    Document,
    DocumentChunk,
    DocumentVersion,
    EvidenceCandidate,
    FactRequirement,
    VerifiedEvidence,
)
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowManualReviewRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.shadow_review import SqlShadowReviewProjectionReader
from sana.platform.db.uow import TenantUnitOfWorkFactory


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def _manifest() -> ShadowManifest:
    strata = (
        (SearchMode.FAST, "zh-CN"),
        (SearchMode.RESEARCH, "zh-CN"),
        (SearchMode.FAST, "en"),
        (SearchMode.RESEARCH, "en"),
        (SearchMode.FAST, "zh-CN"),
        (SearchMode.RESEARCH, "en"),
    )
    cases = tuple(
        ShadowCase(
            id=f"review-{index}",
            prompt=f"review stable prompt {index}",
            locale=locale,
            expected_mode=mode,
            category=CaseCategory.VERSION,
            answerability=Answerability.ANSWERABLE,
            minimum_required_facts=1,
            gold_assertions=(
                GoldAssertion("stable", "normalized_contains_all", ("stable",), False),
            ),
            oracle_type=OracleType.DETERMINISTIC,
            valid_from=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=1),
            required_source_classes=(SourceAuthority.OFFICIAL,),
            forbidden_query_terms=("private memory",),
            must_not_complete=False,
            tags=("review",),
            smoke=True,
        )
        for index, (mode, locale) in enumerate(strata)
    )
    return ShadowManifest("shadow-cases-v1", cases, "d" * 64)


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_review_projection_and_submission_are_exact_owner_only_and_immutable() -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    tenant_id, owner_id, other_user_id = uuid4(), uuid4(), uuid4()
    manifest = _manifest()
    rubric = ReviewRubric("review-rubric-v1")
    rate = CostRate("review-rate-v1", Decimal("1"), Decimal("2"), Decimal("0.006"))
    environment = {"compose_project": "review-test", "network": "isolated"}
    provenance = CampaignProvenance(
        candidate_commit_sha="a" * 40,
        candidate_source_clean=True,
        candidate_image_id=f"sana-candidate@sha256:{'a' * 64}",
        candidate_oci_revision="a" * 64,
        alembic_head="0010_shadow_collector_audit",
        candidate_config_hash="a" * 64,
        harness_commit_sha="b" * 40,
        harness_source_clean=True,
        harness_fileset_hash="b" * 64,
        collector_schema_version="shadow-collector-v1",
        environment_identity_hash=snapshot_hash(environment),
        environment_snapshot=environment,
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Review Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"review-{tenant_id}"},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            for user_id in (owner_id, other_user_id):
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, tenant_id, email, display_name, status) "
                        "VALUES (:id, :tenant, :email, 'Reviewer', 'ACTIVE')"
                    ),
                    {
                        "id": user_id,
                        "tenant": tenant_id,
                        "email": f"{user_id}@example.test",
                    },
                )

        catalog = CampaignPolicyCatalog.standard(
            review_rubrics=(rubric,),
            cost_rates=(rate,),
        )
        command = CreateCampaignCommand(
            tenant_id=tenant_id,
            user_id=owner_id,
            name="review integration",
            idempotency_key="review-integration",
            profile_version=DOCKER_SMOKE_V1.version,
            manifest=manifest,
            review_rubric=rubric,
            cost_rate=rate,
            provenance=provenance,
            retention_until=NOW + timedelta(days=365),
        )
        campaign = await CampaignService(
            uow_factory,
            DeterministicIdFactory("review-campaign"),
            FrozenClock(NOW),
            catalog,
        ).create(command)
        scheduler = CampaignSchedulingService(
            uow_factory,
            FrozenClock(NOW),
            catalog,
        )
        await scheduler.materialize(tenant_id, owner_id, campaign.id, manifest)
        await CampaignLifecycleService(uow_factory, FrozenClock(NOW)).start(
            tenant_id,
            owner_id,
            campaign.id,
        )

        conversation_id, input_id, output_id = uuid4(), uuid4(), uuid4()
        response_id, run_id = uuid4(), uuid4()
        fact_id, document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4(), uuid4()
        candidate_id, verified_id, claim_id, citation_id = uuid4(), uuid4(), uuid4(), uuid4()
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            result_id = await connection.scalar(
                select(ShadowRunResultRecord.id)
                .where(ShadowRunResultRecord.campaign_id == campaign.id)
                .order_by(ShadowRunResultRecord.schedule_ordinal)
                .limit(1)
            )
            assert result_id is not None
            await connection.execute(
                insert(Conversation).values(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    user_id=owner_id,
                    title="review",
                    status="ACTIVE",
                )
            )
            await connection.execute(
                insert(Message).values(
                    id=input_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    author_user_id=owner_id,
                    role="USER",
                    content="private review prompt",
                    message_metadata={},
                    idempotency_key="review-input",
                    created_at=NOW,
                )
            )
            await connection.execute(
                insert(Message).values(
                    id=output_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    role="ASSISTANT",
                    content="private stable answer",
                    message_metadata={"search_run_id": str(run_id)},
                    idempotency_key="review-output",
                    created_at=NOW + timedelta(seconds=2),
                )
            )
            await connection.execute(
                insert(ResponseRun).values(
                    id=response_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    message_id=input_id,
                    status="SUCCEEDED",
                    output_message_id=output_id,
                )
            )
            await connection.execute(
                insert(SearchRunRecord).values(
                    id=run_id,
                    tenant_id=tenant_id,
                    response_run_id=response_id,
                    conversation_id=conversation_id,
                    message_id=input_id,
                    mode="FAST",
                    route_reason_codes=["review"],
                    policy_version="search-v1",
                    route_confidence=1.0,
                    status="SUCCEEDED",
                    answer_quality="COMPLETE",
                    stop_reason="FACTS_COVERED",
                    soft_deadline_at=NOW + timedelta(seconds=10),
                    hard_deadline_at=NOW + timedelta(seconds=15),
                    budget_snapshot={"max_llm_calls": 4},
                    usage_snapshot={},
                    created_at=NOW,
                    started_at=NOW,
                    completed_at=NOW + timedelta(seconds=2),
                    version=2,
                )
            )
            await connection.execute(
                insert(FactRequirement).values(
                    id=fact_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    fact_key="stable",
                    description="stability",
                    required=True,
                    freshness="CURRENT",
                    consequence="HIGH",
                    status="VERIFIED",
                )
            )
            await connection.execute(
                insert(Document).values(
                    id=document_id,
                    tenant_id=tenant_id,
                    canonical_url="https://example.test/stable",
                    canonical_url_hash="1" * 64,
                    title="Stable",
                    source_host="example.test",
                )
            )
            await connection.execute(
                insert(DocumentVersion).values(
                    id=version_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    content_hash="2" * 64,
                    storage_uri="artifact://review-document",
                    media_type="text/plain",
                    language="en",
                    text_length=6,
                    fetched_at=NOW,
                    document_metadata={},
                )
            )
            await connection.execute(
                insert(DocumentChunk).values(
                    id=chunk_id,
                    tenant_id=tenant_id,
                    document_version_id=version_id,
                    ordinal=0,
                    text_content="stable",
                    text_hash=hashlib.sha256(b"stable").hexdigest(),
                    token_count=1,
                    start_offset=0,
                    end_offset=6,
                )
            )
            await connection.execute(
                insert(EvidenceCandidate).values(
                    id=candidate_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    fact_requirement_id=fact_id,
                    document_version_id=version_id,
                    document_chunk_id=chunk_id,
                    quote="stable",
                    quote_hash=hashlib.sha256(b"stable").hexdigest(),
                    start_offset=0,
                    end_offset=6,
                    support_type="SUPPORTS",
                    candidate_score=1.0,
                    source_identity="example.test",
                    source_authority="OFFICIAL",
                )
            )
            await connection.execute(
                insert(VerifiedEvidence).values(
                    id=verified_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    candidate_id=candidate_id,
                    verdict="ACCEPTED",
                    confidence=1.0,
                    reason_codes=["exact"],
                    verifier_version="review-verifier-v1",
                    verified_at=NOW + timedelta(seconds=1),
                )
            )
            await connection.execute(
                insert(AnswerClaim).values(
                    id=claim_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    claim_key="stable",
                    claim_text="private stable claim",
                    support_status="VERIFIED",
                    claim_kind="FACTUAL",
                    fact_requirement_id=fact_id,
                )
            )
            await connection.execute(
                insert(Citation).values(
                    id=citation_id,
                    tenant_id=tenant_id,
                    run_id=run_id,
                    answer_claim_id=claim_id,
                    verified_evidence_id=verified_id,
                    ordinal=1,
                    label="[1]",
                    rendered_url="https://example.test/stable?credential=private",
                    document_version_id=version_id,
                    document_chunk_id=chunk_id,
                    quote="stable",
                    start_offset=0,
                    end_offset=6,
                )
            )
            await connection.execute(
                update(ShadowRunResultRecord)
                .where(ShadowRunResultRecord.id == result_id)
                .values(
                    conversation_id=conversation_id,
                    search_run_id=run_id,
                    manual_review_selected=True,
                    scheduling_state="COLLECTED",
                    actual_mode="FAST",
                    run_status="SUCCEEDED",
                    answer_quality="COMPLETE",
                    source_terminal_at=NOW + timedelta(seconds=2),
                    source_snapshot_digest="3" * 64,
                    collected_at=NOW + timedelta(seconds=3),
                    collector_schema_version="shadow-collector-v1",
                    version=ShadowRunResultRecord.version + 1,
                    updated_at=NOW,
                )
            )
            await connection.execute(
                update(ShadowCampaignRecord)
                .where(ShadowCampaignRecord.id == campaign.id)
                .values(
                    status="AWAITING_REVIEW",
                    review_deadline_at=text("clock_timestamp() + interval '48 hours'"),
                    collected_count=1,
                    version=ShadowCampaignRecord.version + 1,
                    updated_at=NOW,
                )
            )

        projection_reader = SqlShadowReviewProjectionReader(sessions)
        review_service = ShadowReviewService(uow_factory, projection_reader)
        owner = Principal(tenant_id, owner_id, "integration", str(owner_id))
        other = Principal(tenant_id, other_user_id, "integration", str(other_user_id))
        projection = await review_service.projection(owner, campaign.id, result_id)
        assert projection is not None
        assert projection.result_id == result_id
        assert projection.claims[0].citations[0].source_authority == "OFFICIAL"
        assert not hasattr(projection.claims[0], "claim_text")
        assert await review_service.projection(other, campaign.id, result_id) is None

        submission = ReviewSubmission(
            tenant_id,
            campaign.id,
            result_id,
            rubric.version,
            ReviewVerdict.CORRECT,
            ReviewScore.PASS,
            ReviewScore.PASS,
            ReviewScore.PASS,
            ReviewScore.PASS,
            (),
            ReviewActor.HUMAN,
            owner_id,
        )
        receipt = await review_service.submit_human(owner, submission)
        duplicate = await review_service.submit_human(owner, submission)
        assert receipt.duplicate is False and duplicate.duplicate is True
        with pytest.raises(InvariantViolation) as conflict:
            await review_service.submit_human(
                owner,
                ReviewSubmission(
                    tenant_id,
                    campaign.id,
                    result_id,
                    rubric.version,
                    ReviewVerdict.MINOR_ERROR,
                    ReviewScore.PASS,
                    ReviewScore.PASS,
                    ReviewScore.PASS,
                    ReviewScore.FAIL,
                    ("missing_detail",),
                    ReviewActor.HUMAN,
                    owner_id,
                ),
            )
        assert conflict.value.code == "review_conflict"
        with pytest.raises(InvariantViolation) as unauthorized:
            await review_service.submit_human(
                other,
                ReviewSubmission(
                    tenant_id,
                    campaign.id,
                    result_id,
                    rubric.version,
                    ReviewVerdict.CORRECT,
                    ReviewScore.PASS,
                    ReviewScore.PASS,
                    ReviewScore.PASS,
                    ReviewScore.PASS,
                    (),
                    ReviewActor.HUMAN,
                    other_user_id,
                ),
            )
        assert unauthorized.value.code == "review_owner_mismatch"

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            remaining_result_ids = tuple(
                (
                    await connection.scalars(
                        select(ShadowRunResultRecord.id)
                        .where(
                            ShadowRunResultRecord.campaign_id == campaign.id,
                            ShadowRunResultRecord.id != result_id,
                        )
                        .order_by(ShadowRunResultRecord.schedule_ordinal)
                        .limit(2)
                    )
                ).all()
            )
            assert len(remaining_result_ids) == 2
            for index, pending_result_id in enumerate(remaining_result_ids, start=1):
                pending_conversation_id = uuid4()
                pending_message_id = uuid4()
                pending_response_id = uuid4()
                pending_run_id = uuid4()
                await connection.execute(
                    insert(Conversation).values(
                        id=pending_conversation_id,
                        tenant_id=tenant_id,
                        user_id=owner_id,
                        title=f"review system {index}",
                        status="ACTIVE",
                    )
                )
                await connection.execute(
                    insert(Message).values(
                        id=pending_message_id,
                        tenant_id=tenant_id,
                        conversation_id=pending_conversation_id,
                        author_user_id=owner_id,
                        role="USER",
                        content="private expected answer prompt",
                        message_metadata={},
                        idempotency_key=f"review-system-input-{index}",
                        created_at=NOW,
                    )
                )
                await connection.execute(
                    insert(ResponseRun).values(
                        id=pending_response_id,
                        tenant_id=tenant_id,
                        conversation_id=pending_conversation_id,
                        message_id=pending_message_id,
                        status="SUCCEEDED",
                    )
                )
                await connection.execute(
                    insert(SearchRunRecord).values(
                        id=pending_run_id,
                        tenant_id=tenant_id,
                        response_run_id=pending_response_id,
                        conversation_id=pending_conversation_id,
                        message_id=pending_message_id,
                        mode="FAST",
                        route_reason_codes=["review"],
                        policy_version="search-v1",
                        route_confidence=1.0,
                        status="SUCCEEDED",
                        answer_quality="NONE",
                        stop_reason="NO_SUPPORTED_FACTS",
                        soft_deadline_at=NOW + timedelta(seconds=10),
                        hard_deadline_at=NOW + timedelta(seconds=15),
                        budget_snapshot={"max_llm_calls": 4},
                        usage_snapshot={},
                        created_at=NOW,
                        started_at=NOW,
                        completed_at=NOW + timedelta(seconds=2),
                        version=2,
                    )
                )
                await connection.execute(
                    update(ShadowRunResultRecord)
                    .where(ShadowRunResultRecord.id == pending_result_id)
                    .values(
                        conversation_id=pending_conversation_id,
                        search_run_id=pending_run_id,
                        manual_review_selected=True,
                        scheduling_state="COLLECTED",
                        actual_mode="FAST",
                        run_status="SUCCEEDED",
                        answer_quality="NONE",
                        source_terminal_at=NOW + timedelta(seconds=2),
                        source_snapshot_digest=str(index) * 64,
                        collected_at=NOW + timedelta(seconds=3),
                        collector_schema_version="shadow-collector-v1",
                        version=ShadowRunResultRecord.version + 1,
                        updated_at=NOW,
                    )
                )

        system_receipt = await review_service.record_system(
            ReviewSubmission.expected_answer_missing(
                tenant_id=tenant_id,
                campaign_id=campaign.id,
                result_id=remaining_result_ids[0],
                rubric_version=rubric.version,
            )
        )
        assert system_receipt.duplicate is False

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                update(ShadowCampaignRecord)
                .where(ShadowCampaignRecord.id == campaign.id)
                .values(review_deadline_at=text("clock_timestamp() - interval '1 second'"))
            )
        late_submission = ReviewSubmission(
            tenant_id,
            campaign.id,
            remaining_result_ids[1],
            rubric.version,
            ReviewVerdict.MAJOR_ERROR,
            ReviewScore.FAIL,
            ReviewScore.FAIL,
            ReviewScore.FAIL,
            ReviewScore.FAIL,
            ("missing_detail",),
            ReviewActor.HUMAN,
            owner_id,
        )
        with pytest.raises(InvariantViolation) as late:
            await review_service.submit_human(owner, late_submission)
        assert late.value.code == "review_window_closed"
        replay_after_deadline = await review_service.submit_human(owner, submission)
        assert replay_after_deadline.duplicate is True

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            stored = (
                await connection.execute(
                    select(
                        ShadowManualReviewRecord.correctness_verdict,
                        ShadowManualReviewRecord.reason_codes,
                        ShadowManualReviewRecord.reviewer_user_id,
                    ).where(ShadowManualReviewRecord.result_id == result_id)
                )
            ).one()
            await connection.execute(
                update(Citation)
                .where(Citation.id == citation_id)
                .values(start_offset=1, end_offset=7)
            )
        assert stored == ("CORRECT", [], owner_id)
        with pytest.raises(InvariantViolation) as invalid_projection:
            await review_service.projection(owner, campaign.id, result_id)
        assert invalid_projection.value.code == "review_projection_invalid"
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()
