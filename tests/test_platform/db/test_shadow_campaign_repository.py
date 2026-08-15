from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from sana.modules.shadow_campaign.domain import CampaignStatus, snapshot_hash
from sana.modules.shadow_campaign.manifest import ShadowManifest
from sana.modules.shadow_campaign.policy import (
    CampaignPolicyCatalog,
    CostRate,
    DOCKER_SMOKE_V1,
    ReviewRubric,
)
from sana.modules.shadow_campaign.service import (
    CampaignLifecycleService,
    CampaignProvenance,
    CampaignService,
    CreateCampaignCommand,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import DeterministicIdFactory
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 15, tzinfo=UTC)


@pytest.mark.postgres
@pytest.mark.live_network
@pytest.mark.skipif(not DATABASE_URL, reason="SANA_TEST_DATABASE_URL is not configured")
@pytest.mark.asyncio
async def test_campaign_create_retry_and_lifecycle_are_atomic() -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    tenant_id, user_id = uuid4(), uuid4()
    environment = {"compose_project": "task3-integration", "network": "isolated"}
    provenance = CampaignProvenance(
        candidate_commit_sha="a" * 40,
        candidate_source_clean=True,
        candidate_image_id=f"sana-candidate@sha256:{'a' * 64}",
        candidate_oci_revision="a" * 64,
        alembic_head="0009_shadow_campaign_gate",
        candidate_config_hash="a" * 64,
        harness_commit_sha="b" * 40,
        harness_source_clean=True,
        harness_fileset_hash="b" * 64,
        collector_schema_version="shadow-collector-v1",
        environment_identity_hash=snapshot_hash(environment),
        environment_snapshot=environment,
    )
    manifest = ShadowManifest(
        "shadow-cases-v1",
        tuple(SimpleNamespace(smoke=index < 6) for index in range(40)),
        "c" * 64,
    )
    command = CreateCampaignCommand(
        tenant_id=tenant_id,
        user_id=user_id,
        name="repository integration",
        idempotency_key="task3-create",
        profile_version=DOCKER_SMOKE_V1.version,
        manifest=manifest,
        review_rubric=ReviewRubric("review-v1"),
        cost_rate=CostRate(
            "test-rate-v1",
            Decimal("0.1"),
            Decimal("0.2"),
            Decimal("0.001"),
        ),
        provenance=provenance,
        retention_until=NOW + timedelta(days=30),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Campaign Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"campaign-{tenant_id}"},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, display_name, status) "
                    "VALUES (:id, :tenant, :email, 'Campaign User', 'ACTIVE')"
                ),
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "email": f"{user_id}@example.test",
                },
            )

        uow_factory = TenantUnitOfWorkFactory(sessions)
        service = CampaignService(
            uow_factory,
            DeterministicIdFactory("campaign-integration"),
            FrozenClock(NOW),
            CampaignPolicyCatalog.standard(
                review_rubrics=(command.review_rubric,),
                cost_rates=(command.cost_rate,),
            ),
        )
        receipts = await asyncio.gather(
            service.create(command),
            service.create(command),
        )
        first = next(item for item in receipts if not item.duplicate)
        duplicate = next(item for item in receipts if item.duplicate)
        lifecycle = CampaignLifecycleService(uow_factory, FrozenClock(NOW))
        started = await lifecycle.start(tenant_id, user_id, first.id)

        assert duplicate.id == first.id
        assert duplicate.duplicate is True
        assert started is not None
        assert started.status is CampaignStatus.RUNNING
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            row = (
                await connection.execute(
                    text(
                        "SELECT status, creation_request_hash, profile_hash, version "
                        "FROM shadow_campaigns WHERE id = :id"
                    ),
                    {"id": first.id},
                )
            ).one()
        assert row == (
            "RUNNING",
            first.request_hash,
            DOCKER_SMOKE_V1.sha256,
            1,
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()
