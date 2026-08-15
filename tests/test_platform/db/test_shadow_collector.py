from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text, update

from sana.app.shadow_collector import ShadowCollectorService
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.orchestration.domain import SearchMode
from sana.modules.shadow_campaign.budget import CampaignBudgetService
from sana.modules.shadow_campaign.collector import collect_run_snapshot
from sana.modules.shadow_campaign.domain import ReservationState, snapshot_hash
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
from sana.modules.shadow_campaign.report import CampaignReportBuilder
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
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.orchestration import (
    OutboxEvent,
    SearchRunRecord,
    SearchStepRecord,
    StepAttemptRecord,
)
from sana.platform.db.models.search import (
    AnswerClaim,
    Citation,
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionFetch,
    EvidenceCandidate,
    FactRequirement,
    FetchArtifact,
    ProviderAttempt,
    QuerySpec,
    VerifiedEvidence,
)
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowGoldAssertionResultRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.shadow_collector import SqlShadowSnapshotReader
from sana.platform.db.shadow_report import SqlShadowReportGateway
from sana.platform.db.uow import TenantUnitOfWorkFactory


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


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
            id=f"collector-{index}",
            prompt=f"collector stable prompt {index}",
            locale=locale,
            expected_mode=mode,
            category=CaseCategory.VERSION,
            answerability=Answerability.ANSWERABLE,
            minimum_required_facts=1,
            gold_assertions=(
                GoldAssertion(
                    "stable-answer",
                    "normalized_contains_all",
                    ("stable",),
                    False,
                ),
            ),
            oracle_type=OracleType.DETERMINISTIC,
            valid_from=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=1),
            required_source_classes=(SourceAuthority.OFFICIAL,),
            forbidden_query_terms=("private memory",),
            must_not_complete=False,
            tags=("collector",),
            smoke=True,
        )
        for index, (mode, locale) in enumerate(strata)
    )
    return ShadowManifest("shadow-cases-v1", cases, "c" * 64)


async def _seed_terminal_source(
    engine,
    *,
    tenant_id,
    user_id,
    campaign_id,
    result_id,
) -> tuple:
    conversation_id = uuid4()
    input_message_id = uuid4()
    output_message_id = uuid4()
    response_run_id = uuid4()
    search_run_id = uuid4()
    step_id = uuid4()
    attempt_id = uuid4()
    invocation_id = uuid4()
    fact_id = uuid4()
    query_id = uuid4()
    provider_attempt_id = uuid4()
    fetch_artifact_id = uuid4()
    document_id = uuid4()
    document_version_id = uuid4()
    chunk_id = uuid4()
    candidate_id = uuid4()
    verified_id = uuid4()
    claim_id = uuid4()
    citation_id = uuid4()
    outbox_id = uuid4()
    completed_at = NOW + timedelta(seconds=2)
    async with engine.begin() as connection:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": str(tenant_id)},
        )
        await connection.execute(
            insert(Conversation).values(
                id=conversation_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="collector",
                status="ACTIVE",
            )
        )
        await connection.execute(
            insert(Message).values(
                id=input_message_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                author_user_id=user_id,
                role="USER",
                content="collector input",
                message_metadata={},
                idempotency_key="collector-input",
                created_at=NOW,
            )
        )
        await connection.execute(
            insert(Message).values(
                id=output_message_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                author_user_id=None,
                role="ASSISTANT",
                content="The release is stable.",
                message_metadata={"search_run_id": str(search_run_id)},
                idempotency_key="collector-output",
                created_at=completed_at,
            )
        )
        await connection.execute(
            insert(ResponseRun).values(
                id=response_run_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                message_id=input_message_id,
                status="SUCCEEDED",
                output_message_id=output_message_id,
            )
        )
        await connection.execute(
            insert(SearchRunRecord).values(
                id=search_run_id,
                tenant_id=tenant_id,
                response_run_id=response_run_id,
                conversation_id=conversation_id,
                message_id=input_message_id,
                mode="FAST",
                route_reason_codes=["collector"],
                policy_version="search-v1",
                route_confidence=1.0,
                status="SUCCEEDED",
                answer_quality="COMPLETE",
                stop_reason="FACTS_COVERED",
                soft_deadline_at=NOW + timedelta(seconds=10),
                hard_deadline_at=NOW + timedelta(seconds=15),
                budget_snapshot={"max_llm_calls": 4},
                usage_snapshot={
                    "llm_call_count": 1,
                    "prompt_token_count": 100,
                    "completion_token_count": 50,
                },
                created_at=NOW,
                started_at=NOW,
                completed_at=completed_at,
                version=4,
            )
        )
        await connection.execute(
            insert(SearchStepRecord).values(
                id=step_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                step_key="synthesize",
                step_type="SYNTHESIZE",
                plan_revision=1,
                status="SUCCEEDED",
                input_ref={"uri": "artifact://input", "sha256": "1" * 64},
                output_ref={"uri": "artifact://output", "sha256": "2" * 64},
                retry_at=None,
                version=2,
            )
        )
        await connection.execute(
            insert(StepAttemptRecord).values(
                id=attempt_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                step_id=step_id,
                attempt_no=1,
                idempotency_key=f"collector-attempt:{attempt_id}",
                lease_owner="candidate-worker",
                leased_until=completed_at,
                deadline_at=NOW + timedelta(seconds=15),
                started_at=NOW,
                completed_at=completed_at,
                input_ref={"uri": "artifact://input", "sha256": "1" * 64},
                output_ref={"uri": "artifact://output", "sha256": "2" * 64},
            )
        )
        await connection.execute(
            insert(ModelInvocationRecord).values(
                id=invocation_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                role="SYNTHESIZER",
                provider="deepseek",
                model="deepseek-chat",
                call_no=1,
                logical_call_key="collector-call",
                status="COMPLETED",
                billing_disposition="BILLED",
                provider_called=True,
                trace_id="1" * 32,
                span_id="2" * 16,
                prompt_template_version="collector-v1",
                parser_schema_version="collector-v1",
                output_format="text",
                thinking_mode="disabled",
                input_chars=100,
                output_chars=25,
                prompt_tokens=100,
                completion_tokens=50,
                started_at=NOW,
                completed_at=completed_at,
            )
        )
        await connection.execute(
            insert(FactRequirement).values(
                id=fact_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                fact_key="release",
                description="release stability",
                required=True,
                freshness="CURRENT",
                consequence="HIGH",
                status="VERIFIED",
            )
        )
        await connection.execute(
            insert(QuerySpec).values(
                id=query_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                fact_requirement_id=fact_id,
                plan_revision=1,
                query_key="release-query",
                query_text="stable release",
                provider_class="direct",
                locale="zh-CN",
                query_metadata={},
            )
        )
        await connection.execute(
            insert(ProviderAttempt).values(
                id=provider_attempt_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                query_spec_id=query_id,
                attempt_no=1,
                provider="direct",
                status="SUCCEEDED",
                started_at=NOW,
                completed_at=completed_at,
                latency_ms=10,
            )
        )
        await connection.execute(
            insert(Document).values(
                id=document_id,
                tenant_id=tenant_id,
                canonical_url="https://example.test/release",
                canonical_url_hash="3" * 64,
                title="Release",
                source_host="example.test",
            )
        )
        await connection.execute(
            insert(FetchArtifact).values(
                id=fetch_artifact_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                search_hit_id=None,
                url="https://example.test/release",
                url_hash="3" * 64,
                attempt_no=1,
                fetcher="test",
                status="SUCCEEDED",
                http_status=200,
                media_type="text/plain",
                content_hash="4" * 64,
                storage_uri="artifact://fetch",
                response_bytes=6,
                fetched_at=NOW,
                fetch_metadata={},
            )
        )
        await connection.execute(
            insert(DocumentVersion).values(
                id=document_version_id,
                tenant_id=tenant_id,
                document_id=document_id,
                fetch_artifact_id=fetch_artifact_id,
                content_hash="4" * 64,
                storage_uri="artifact://document",
                media_type="text/plain",
                language="en",
                text_length=6,
                fetched_at=NOW,
                document_metadata={},
            )
        )
        await connection.execute(
            insert(DocumentVersionFetch).values(
                id=uuid4(),
                tenant_id=tenant_id,
                run_id=search_run_id,
                document_version_id=document_version_id,
                fetch_artifact_id=fetch_artifact_id,
                created_at=NOW,
            )
        )
        await connection.execute(
            insert(DocumentChunk).values(
                id=chunk_id,
                tenant_id=tenant_id,
                document_version_id=document_version_id,
                ordinal=0,
                text_content="stable",
                text_hash="5" * 64,
                token_count=1,
                start_offset=0,
                end_offset=6,
            )
        )
        await connection.execute(
            insert(EvidenceCandidate).values(
                id=candidate_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                fact_requirement_id=fact_id,
                document_version_id=document_version_id,
                document_chunk_id=chunk_id,
                quote="stable",
                quote_hash="6" * 64,
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
                run_id=search_run_id,
                candidate_id=candidate_id,
                verdict="ACCEPTED",
                confidence=1.0,
                reason_codes=["exact"],
                verifier_version="verifier-v1",
                verified_at=completed_at,
            )
        )
        await connection.execute(
            insert(AnswerClaim).values(
                id=claim_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                claim_key="release",
                claim_text="The release is stable.",
                support_status="VERIFIED",
                claim_kind="FACTUAL",
                fact_requirement_id=fact_id,
            )
        )
        await connection.execute(
            insert(Citation).values(
                id=citation_id,
                tenant_id=tenant_id,
                run_id=search_run_id,
                answer_claim_id=claim_id,
                verified_evidence_id=verified_id,
                ordinal=1,
                label="[1]",
                rendered_url="https://example.test/release?private=ignored",
                document_version_id=document_version_id,
                document_chunk_id=chunk_id,
                quote="stable",
                start_offset=0,
                end_offset=6,
            )
        )
        await connection.execute(
            insert(OutboxEvent).values(
                id=outbox_id,
                tenant_id=tenant_id,
                aggregate_type="search_step",
                aggregate_id=step_id,
                event_type="STEP_READY_FAST",
                payload={"private": "must not be collected"},
                trace_context={},
                dedupe_key=f"collector-outbox:{outbox_id}",
                available_at=NOW,
                created_at=NOW,
                published_at=NOW + timedelta(milliseconds=1),
                publish_attempts=1,
            )
        )
        await connection.execute(
            update(ShadowRunResultRecord)
            .where(ShadowRunResultRecord.id == result_id)
            .values(
                conversation_id=conversation_id,
                search_run_id=search_run_id,
                scheduling_state="SUBMITTED",
                lease_owner=None,
                lease_expires_at=None,
                submission_attempt_count=1,
                version=ShadowRunResultRecord.version + 1,
                updated_at=NOW,
            )
        )
        await connection.execute(
            update(ShadowCampaignRecord)
            .where(ShadowCampaignRecord.id == campaign_id)
            .values(
                submitted_count=ShadowCampaignRecord.submitted_count + 1,
                version=ShadowCampaignRecord.version + 1,
                updated_at=NOW,
            )
        )
    return conversation_id, search_run_id


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_collector_is_fenced_atomic_idempotent_and_rls_scoped() -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    tenant_id, user_id = uuid4(), uuid4()
    manifest = _manifest()
    review = ReviewRubric("collector-review-v1")
    rate = CostRate(
        "collector-rate-v1",
        Decimal("1"),
        Decimal("2"),
        Decimal("0.006"),
    )
    environment = {"compose_project": "collector-test", "network": "isolated"}
    provenance = CampaignProvenance(
        candidate_commit_sha="a" * 40,
        candidate_source_clean=True,
        candidate_image_id=f"sana-candidate@sha256:{'a' * 64}",
        candidate_oci_revision="a" * 40,
        alembic_head="0010_shadow_collector_audit",
        candidate_config_hash="a" * 64,
        harness_commit_sha="b" * 40,
        harness_source_clean=True,
        harness_fileset_hash="b" * 64,
        collector_schema_version="shadow-collector-v2",
        environment_identity_hash=snapshot_hash(environment),
        environment_snapshot=environment,
    )
    command = CreateCampaignCommand(
        tenant_id=tenant_id,
        user_id=user_id,
        name="collector integration",
        idempotency_key="collector-integration",
        profile_version=DOCKER_SMOKE_V1.version,
        manifest=manifest,
        review_rubric=review,
        cost_rate=rate,
        provenance=provenance,
        retention_until=NOW + timedelta(days=365),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Collector Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"collector-{tenant_id}"},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, display_name, status) "
                    "VALUES (:id, :tenant, :email, 'Collector User', 'ACTIVE')"
                ),
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "email": f"{user_id}@example.test",
                },
            )
        catalog = CampaignPolicyCatalog.standard(
            review_rubrics=(review,),
            cost_rates=(rate,),
        )
        campaign_service = CampaignService(
            uow_factory,
            DeterministicIdFactory("collector-campaign"),
            FrozenClock(NOW),
            catalog,
        )
        campaign = await campaign_service.create(command)
        scheduler = CampaignSchedulingService(
            uow_factory,
            FrozenClock(NOW),
            catalog,
        )
        await scheduler.materialize(tenant_id, user_id, campaign.id, manifest)
        await CampaignLifecycleService(uow_factory, FrozenClock(NOW)).start(
            tenant_id,
            user_id,
            campaign.id,
        )
        run_lease = await scheduler.claim_next(
            tenant_id,
            campaign.id,
            "candidate-worker",
        )
        assert run_lease is not None
        reservation = await CampaignBudgetService(uow_factory).reserve_run(run_lease)
        assert reservation.allowed and run_lease.reservation_state is ReservationState.ACTIVE
        await _seed_terminal_source(
            engine,
            tenant_id=tenant_id,
            user_id=user_id,
            campaign_id=campaign.id,
            result_id=run_lease.id,
        )

        reader = SqlShadowSnapshotReader(sessions)
        collector = ShadowCollectorService(
            uow_factory,
            reader,
            lease_duration=timedelta(seconds=30),
        )
        lease = await collector.claim_next(tenant_id, campaign.id, "collector-a")
        assert lease is not None
        assert await collector.claim_next(tenant_id, campaign.id, "collector-b") is None
        assert await collector.claim_next(uuid4(), campaign.id, "cross-tenant") is None

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                update(ShadowRunResultRecord)
                .where(ShadowRunResultRecord.id == lease.id)
                .values(
                    collector_lease_expires_at=text(
                        "clock_timestamp() - interval '1 second'"
                    )
                )
            )
        reclaimed = await collector.claim_next(tenant_id, campaign.id, "collector-b")
        assert reclaimed is not None and reclaimed.version > lease.version
        with pytest.raises(InvariantViolation) as stale:
            await reader.read(lease)
        assert stale.value.code == "collector_source_binding_changed"
        lease = reclaimed

        snapshot = await reader.read(lease)
        case = next(item for item in manifest.cases if item.id == lease.case_id)
        outcome = collect_run_snapshot(
            snapshot,
            case,
            lease.cost_rate,
            oracle_version=manifest.version,
            collector_schema_version=lease.collector_schema_version,
        )
        async with uow_factory(tenant_id) as uncommitted:
            await uncommitted.campaign_collector.persist(lease, outcome)

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            rolled_back = (
                await connection.execute(
                    select(
                        ShadowRunResultRecord.scheduling_state,
                        ShadowRunResultRecord.reservation_state,
                        ShadowRunResultRecord.source_snapshot_digest,
                    ).where(ShadowRunResultRecord.id == lease.id)
                )
            ).one()
        assert rolled_back == ("SUBMITTED", "ACTIVE", None)

        receipt = await collector.collect(lease, manifest)
        duplicate = await collector.collect(lease, manifest)
        assert receipt.duplicate is False
        assert duplicate.duplicate is True
        assert duplicate.source_snapshot_digest == receipt.source_snapshot_digest

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            result = (
                await connection.execute(
                    select(
                        ShadowRunResultRecord.scheduling_state,
                        ShadowRunResultRecord.reservation_state,
                        ShadowRunResultRecord.collector_attempt_count,
                        ShadowRunResultRecord.collector_lease_owner,
                        ShadowRunResultRecord.source_snapshot_digest,
                        ShadowRunResultRecord.model_call_count,
                        ShadowRunResultRecord.prompt_tokens,
                        ShadowRunResultRecord.completion_tokens,
                        ShadowRunResultRecord.gold_assertion_passed,
                        ShadowRunResultRecord.traceability_violation_count,
                    ).where(ShadowRunResultRecord.id == lease.id)
                )
            ).one()
            campaign_row = (
                await connection.execute(
                    select(
                        ShadowCampaignRecord.collected_count,
                        ShadowCampaignRecord.observed_provider_calls,
                        ShadowCampaignRecord.observed_prompt_tokens,
                        ShadowCampaignRecord.observed_completion_tokens,
                        ShadowCampaignRecord.reserved_provider_calls,
                    ).where(ShadowCampaignRecord.id == campaign.id)
                )
            ).one()
            gold = (
                await connection.execute(
                    select(
                        ShadowGoldAssertionResultRecord.assertion_id,
                        ShadowGoldAssertionResultRecord.status,
                        ShadowGoldAssertionResultRecord.reason_code,
                    ).where(ShadowGoldAssertionResultRecord.result_id == lease.id)
                )
            ).all()
        assert result == (
            "COLLECTED",
            "SETTLED",
            2,
            None,
            receipt.source_snapshot_digest,
            1,
            100,
            50,
            1,
            0,
        )
        assert campaign_row == (1, 1, 100, 50, 0)
        assert gold == [("stable-answer", "PASS", "assertion_passed")]

        report_snapshot = await SqlShadowReportGateway(sessions, reader).read(
            tenant_id,
            user_id,
            campaign.id,
        )
        assert report_snapshot is not None
        measured = next(
            item
            for item in report_snapshot.decision_input["results"]
            if item["result_id"] == lease.id
        )
        assert measured["current_source_digest"] == receipt.source_snapshot_digest
        report = CampaignReportBuilder().prepare(report_snapshot)
        assert not any(
            rule.rule_id == "hard_source_snapshot_mismatch" and not rule.passed
            for rule in report.decision.rules
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()
