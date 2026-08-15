from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import hashlib
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update

from sana.app.shadow_report import ShadowReportService
from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.domain import GateStatus, snapshot_hash
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    DOCKER_SMOKE_V1,
    CostRate,
    ReviewRubric,
)
from sana.modules.shadow_campaign.report import CampaignReportBuilder, FinalReportBinding
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
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.shadow_report import SqlShadowReportGateway
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.storage.campaign_reports import LocalCampaignReportStore

from tests.test_platform.db.test_shadow_review import NOW, _manifest


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_report_gateway_fences_stale_input_and_binds_one_final_report(tmp_path) -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    tenant_id, owner_id, other_user_id = uuid4(), uuid4(), uuid4()
    manifest = _manifest()
    rubric = ReviewRubric("report-rubric-v1")
    rate = CostRate("report-rate-v1", Decimal("1"), Decimal("2"), Decimal("0.006"))
    environment = {"compose_project": "report-test", "network": "isolated"}
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
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Report Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"report-{tenant_id}"},
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
                        "VALUES (:id, :tenant, :email, 'Reporter', 'ACTIVE')"
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
            name="report integration",
            idempotency_key="report-integration",
            profile_version=DOCKER_SMOKE_V1.version,
            manifest=manifest,
            review_rubric=rubric,
            cost_rate=rate,
            provenance=provenance,
            retention_until=NOW + timedelta(days=365),
        )
        campaign = await CampaignService(
            uow_factory,
            DeterministicIdFactory("report-campaign"),
            FrozenClock(NOW),
            catalog,
        ).create(command)
        await CampaignSchedulingService(
            uow_factory,
            FrozenClock(NOW),
            catalog,
        ).materialize(tenant_id, owner_id, campaign.id, manifest)
        await CampaignLifecycleService(uow_factory, FrozenClock(NOW)).start(
            tenant_id,
            owner_id,
            campaign.id,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                update(ShadowRunResultRecord)
                .where(ShadowRunResultRecord.campaign_id == campaign.id)
                .values(
                    scheduling_state="SKIPPED",
                    stable_skip_reason="controlled_test_stop",
                    version=ShadowRunResultRecord.version + 1,
                    updated_at=NOW,
                )
            )
            await connection.execute(
                update(ShadowCampaignRecord)
                .where(ShadowCampaignRecord.id == campaign.id)
                .values(
                    skipped_count=6,
                    version=ShadowCampaignRecord.version + 1,
                    updated_at=NOW,
                )
            )

        gateway = SqlShadowReportGateway(sessions)
        owner = Principal(tenant_id, owner_id, "integration", str(owner_id))
        snapshot = await gateway.read(tenant_id, owner_id, campaign.id)
        assert snapshot is not None
        assert await gateway.read(tenant_id, other_user_id, campaign.id) is None
        prepared = CampaignReportBuilder().prepare(snapshot)
        assert prepared.decision.status is GateStatus.INSUFFICIENT_SAMPLE
        assert prepared.finalizable

        store = LocalCampaignReportStore(tmp_path)
        json_uri = await store.put(
            tenant_id,
            campaign.id,
            prepared.json_bytes,
            media_type="application/json",
        )
        markdown_uri = await store.put(
            tenant_id,
            campaign.id,
            prepared.markdown_bytes,
            media_type="text/markdown",
        )
        stale_binding = FinalReportBinding(
            tenant_id,
            campaign.id,
            owner_id,
            prepared.campaign_status,
            prepared.campaign_version,
            prepared.decision_input_hash,
            prepared.decision_hash,
            prepared.decision.status,
            prepared.automatic_gate_status,
            prepared.manual_review_status,
            prepared.finalization_reason or "final",
            json_uri,
            hashlib.sha256(prepared.json_bytes).hexdigest(),
            markdown_uri,
            hashlib.sha256(prepared.markdown_bytes).hexdigest(),
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            first_result_id = await connection.scalar(
                select(ShadowRunResultRecord.id)
                .where(ShadowRunResultRecord.campaign_id == campaign.id)
                .order_by(ShadowRunResultRecord.schedule_ordinal)
                .limit(1)
            )
            await connection.execute(
                update(ShadowRunResultRecord)
                .where(ShadowRunResultRecord.id == first_result_id)
                .values(
                    stable_skip_reason="controlled_test_stop_changed",
                    version=ShadowRunResultRecord.version + 1,
                )
            )
        with pytest.raises(InvariantViolation) as stale:
            await gateway.bind(stale_binding)
        assert stale.value.code == "report_input_stale"

        service = ShadowReportService(gateway, store)
        first = await service.generate(owner, campaign.id)
        second = await service.generate(owner, campaign.id)
        assert first is not None and second is not None
        assert first.final and second.final and second.duplicate
        assert first.decision_hash == second.decision_hash

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            stored = (
                await connection.execute(
                    select(
                        ShadowCampaignRecord.status,
                        ShadowCampaignRecord.gate_status,
                        ShadowCampaignRecord.decision_hash,
                        ShadowCampaignRecord.final_json_uri,
                    ).where(ShadowCampaignRecord.id == campaign.id)
                )
            ).one()
            assert stored.status == "COMPLETED"
            assert stored.gate_status == "INSUFFICIENT_SAMPLE"
            assert stored.decision_hash == first.decision_hash
            assert stored.final_json_uri == first.json_uri
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()


def test_report_gateway_defensively_rejects_paused_final_binding() -> None:
    for campaign in (
        SimpleNamespace(status="PAUSED", stop_intent="PAUSE"),
        SimpleNamespace(status="STOPPING", stop_intent="PAUSE"),
    ):
        with pytest.raises(InvariantViolation) as captured:
            SqlShadowReportGateway._terminal_status(campaign)

        assert captured.value.code == "paused_campaign_finalization_forbidden"


def test_report_revalidation_excludes_post_collection_budget_signal() -> None:
    result = SimpleNamespace(
        source_snapshot_digest="a" * 64,
        collector_schema_version="shadow-collector-v2",
        source_terminal_at=NOW,
        actual_mode="RESEARCH",
        run_status="SUCCEEDED",
        answer_quality="COMPLETE",
        run_stop_reason=None,
        latency_ms=1,
        minimum_required_facts=1,
        fact_total=1,
        fact_covered=1,
        fact_gap=0,
        plan_completeness_failure=False,
        factual_claim_count=1,
        nonfactual_claim_count=0,
        cited_factual_claim_count=1,
        valid_citation_chain_count=1,
        traceability_violation_count=0,
        oracle_version=None,
        query_pollution_count=0,
        model_call_count=1,
        settled_observed_provider_calls=1,
        prompt_tokens=1,
        completion_tokens=1,
        settled_observed_cost=Decimal("0.001"),
        possibly_billed_call_charge=0,
        possibly_billed_cost_charge=Decimal("0"),
        degraded=False,
        provider_success_count=1,
        provider_failure_count=0,
        error_class="CANDIDATE_DEFECT",
        error_code="budget_violation",
        failed_phase=None,
        error_signal_flags=["budget_violation", "content_gap"],
    )

    outcome = SqlShadowReportGateway._stored_outcome(result, ())

    assert outcome.error_signal_flags == ("content_gap",)
