from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from sana.app.api.services import DatabaseConversationCatalogService
from sana.modules.conversation.domain import (
    ConversationService,
    SubmitMessageCommand,
)
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import RoutingDecision, SearchMode
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shadow_campaign.budget import (
    CampaignBudgetService,
    SettlementUsage,
)
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ReservationState,
    snapshot_hash,
)
from sana.modules.shadow_campaign.execution import (
    CampaignExecutionService,
    CandidateSubmissionReceipt,
)
from sana.modules.shadow_campaign.manifest import (
    Answerability,
    CaseCategory,
    ShadowManifest,
)
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
from sana.modules.shadow_campaign.scheduler import CampaignSchedulingService
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.errors import InvariantViolation
from sana.modules.shared.ids import (
    DeterministicIdFactory,
    RandomIdFactory,
    TraceContext,
)
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.shadow_campaign_repository import SqlShadowCampaignRepository
from sana.platform.db.uow import TenantUnitOfWorkFactory


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 15, tzinfo=UTC)


def test_run_budget_reservation_uses_explicit_frozen_cost_envelope() -> None:
    current = SimpleNamespace(
        provider_call_structural_ceiling=48,
        max_runs=6,
        cost_rate_snapshot={
            "possibly_billed_run_reserve_usd": "0.001",
            "run_reservation_usd": "0.002",
        },
    )
    legacy = SimpleNamespace(
        provider_call_structural_ceiling=48,
        max_runs=6,
        cost_rate_snapshot={"possibly_billed_run_reserve_usd": "0.001"},
    )

    assert SqlShadowCampaignRepository._reservation_request(
        current
    ).estimated_cost == Decimal("0.002")
    assert SqlShadowCampaignRepository._reservation_request(
        legacy
    ).estimated_cost == Decimal("0.001")


class InProcessCandidateGateway:
    def __init__(
        self,
        principal: Principal,
        catalog: DatabaseConversationCatalogService,
        conversations: ConversationService,
        policy_version: str,
    ) -> None:
        self._principal = principal
        self._catalog = catalog
        self._conversations = conversations
        self._policy_version = policy_version
        self.fail_next_submission = False

    async def create_conversation(
        self,
        *,
        title: str,
        idempotency_key: str,
    ):
        created = await self._catalog.create(
            self._principal,
            title,
            idempotency_key,
        )
        return created.id

    async def submit_message(
        self,
        *,
        conversation_id,
        content: str,
        idempotency_key: str,
    ) -> CandidateSubmissionReceipt:
        if self.fail_next_submission:
            self.fail_next_submission = False
            raise TimeoutError("simulated failure before Message API acceptance")
        receipt = await self._conversations.submit_message(
            SubmitMessageCommand(
                tenant_id=self._principal.tenant_id,
                user_id=self._principal.user_id,
                conversation_id=conversation_id,
                content=content,
                idempotency_key=idempotency_key,
                routing=RoutingDecision(
                    SearchMode.FAST,
                    ("shadow_integration",),
                    self._policy_version,
                    1.0,
                ),
                trace_context=TraceContext.create(),
            )
        )
        return CandidateSubmissionReceipt(
            receipt.message_id,
            receipt.response_run_id,
            receipt.search_run_id,
        )


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
        candidate_oci_revision="a" * 40,
        alembic_head="0009_shadow_campaign_gate",
        candidate_config_hash="a" * 64,
        harness_commit_sha="b" * 40,
        harness_source_clean=True,
        harness_fileset_hash="b" * 64,
        collector_schema_version="shadow-collector-v2",
        environment_identity_hash=snapshot_hash(environment),
        environment_snapshot=environment,
    )
    cases = tuple(
        SimpleNamespace(
            id=f"case-{index:02d}",
            prompt=f"integration prompt {index}",
            expected_mode=(
                SearchMode.FAST
                if index < 3 or index >= 6 and index % 2 == 0
                else SearchMode.RESEARCH
            ),
            locale="zh-CN" if index % 2 == 0 else "en",
            category=CaseCategory.VERSION,
            answerability=Answerability.ANSWERABLE,
            smoke=index < 6,
        )
        for index in range(40)
    )
    manifest = ShadowManifest("shadow-cases-v1", cases, "c" * 64)
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
            Decimal("0.006"),
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
        clock = FrozenClock(NOW)
        catalog = CampaignPolicyCatalog.standard(
            review_rubrics=(command.review_rubric,),
            cost_rates=(command.cost_rate,),
        )
        service = CampaignService(
            uow_factory,
            DeterministicIdFactory("campaign-integration"),
            clock,
            catalog,
        )
        receipts = await asyncio.gather(
            service.create(command),
            service.create(command),
        )
        first = next(item for item in receipts if not item.duplicate)
        duplicate = next(item for item in receipts if item.duplicate)
        scheduler = CampaignSchedulingService(uow_factory, clock, catalog)
        materialized = await scheduler.materialize(
            tenant_id,
            user_id,
            first.id,
            manifest,
        )
        duplicate_plan = await scheduler.materialize(
            tenant_id,
            user_id,
            first.id,
            manifest,
        )
        lifecycle = CampaignLifecycleService(uow_factory, clock)
        started = await lifecycle.start(tenant_id, user_id, first.id)
        claimed = await asyncio.gather(
            scheduler.claim_next(tenant_id, first.id, "worker-a"),
            scheduler.claim_next(tenant_id, first.id, "worker-b"),
            scheduler.claim_next(tenant_id, first.id, "worker-c"),
        )

        assert duplicate.id == first.id
        assert duplicate.duplicate is True
        assert materialized is not None and materialized.planned_count == 6
        assert duplicate_plan is not None and duplicate_plan.duplicate
        assert started is not None
        assert started.status is CampaignStatus.RUNNING
        active = [item for item in claimed if item is not None]
        assert len(active) == 2
        assert {item.schedule_ordinal for item in active} == {1, 2}
        budget = CampaignBudgetService(uow_factory)
        first_lease = next(item for item in active if item.schedule_ordinal == 1)
        second_lease = next(item for item in active if item.schedule_ordinal == 2)
        first_reservation = await budget.reserve_run(first_lease)
        assert first_reservation.allowed is True
        assert first_reservation.reserved_provider_calls == 8
        assert first_reservation.reserved_estimated_cost == Decimal("0.006")
        assert first_lease.reservation_state is ReservationState.ACTIVE

        search_policy = SearchPolicy.default()
        principal = Principal(tenant_id, user_id, "integration", str(user_id))
        candidate = InProcessCandidateGateway(
            principal,
            DatabaseConversationCatalogService(
                uow_factory,
                clock,
                RandomIdFactory(),
            ),
            ConversationService(
                uow_factory,
                RandomIdFactory(),
                clock,
                search_policy,
            ),
            search_policy.version,
        )
        execution = CampaignExecutionService(uow_factory, candidate)
        prompt_by_case = {item.id: item.prompt for item in cases}
        candidate.fail_next_submission = True
        with pytest.raises(TimeoutError, match="before Message API"):
            await execution.execute(
                first_lease,
                prompt_by_case[first_lease.case_id],
            )
        assert first_lease.state.value == "CONVERSATION_BOUND"
        bound_conversation_id = first_lease.conversation_id
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_run_results "
                    "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                    "WHERE id = :result"
                ),
                {"result": first_lease.id},
            )
        recovered_first = await scheduler.claim_next(
            tenant_id,
            first.id,
            "worker-recovery",
        )
        assert recovered_first is not None
        assert recovered_first.state.value == "CONVERSATION_BOUND"
        assert recovered_first.conversation_id == bound_conversation_id
        submission = await execution.execute(
            recovered_first,
            prompt_by_case[recovered_first.case_id],
        )
        assert submission.result_id == recovered_first.id
        # A submitted run with an ACTIVE budget reservation remains in-flight.
        # Together with the still-claimed second unit it must fence a third claim.
        assert (
            await scheduler.claim_next(tenant_id, first.id, "worker-overflow")
            is None
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "UPDATE search_runs SET status = 'FAILED', "
                    "stop_reason = 'INTEGRATION_TERMINAL', completed_at = clock_timestamp() "
                    "WHERE id = :run"
                ),
                {"run": submission.search_run_id},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_run_results "
                    "SET source_terminal_at = clock_timestamp(), "
                    "version = version + 1, updated_at = clock_timestamp() "
                    "WHERE id = :result"
                ),
                {"result": recovered_first.id},
            )
        first_settlement = await budget.settle_result(
            tenant_id,
            first.id,
            recovered_first.id,
            source_snapshot_digest="c" * 64,
            usage=SettlementUsage(0, 0, 0, Decimal("0")),
        )
        assert first_settlement.duplicate is False

        second_reservation = await budget.reserve_run(second_lease)
        released = await budget.release_run(second_lease)
        duplicate_release = await budget.release_run(second_lease)
        assert second_reservation.allowed is True
        assert released.duplicate is False
        assert duplicate_release.duplicate is True
        assert second_lease.reservation_state is ReservationState.RELEASED
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_run_results "
                    "SET scheduling_state = 'SKIPPED', lease_owner = NULL, "
                    "lease_expires_at = NULL, version = version + 1, "
                    "updated_at = clock_timestamp() "
                    "WHERE id = :result"
                ),
                {"result": second_lease.id},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_campaigns SET skipped_count = skipped_count + 1, "
                    "version = version + 1, updated_at = clock_timestamp() "
                    "WHERE id = :campaign"
                ),
                {"campaign": first.id},
            )
        next_claims = await asyncio.gather(
            scheduler.claim_next(tenant_id, first.id, "worker-d"),
            scheduler.claim_next(tenant_id, first.id, "worker-e"),
        )
        next_active = sorted(
            (item for item in next_claims if item is not None),
            key=lambda item: item.schedule_ordinal,
        )
        assert [item.schedule_ordinal for item in next_active] == [3, 4]
        third_lease, fourth_lease = next_active
        third_reservation = await budget.reserve_run(third_lease)
        assert third_reservation.allowed is True
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_run_results "
                    "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                    "WHERE campaign_id = :campaign "
                    "AND scheduling_state IN ('CLAIMED', 'CONVERSATION_BOUND')"
                ),
                {"campaign": first.id},
            )
        reclaimed = await scheduler.claim_next(tenant_id, first.id, "worker-f")
        assert reclaimed is not None and reclaimed.schedule_ordinal == 3
        assert reclaimed.version > third_lease.version
        assert reclaimed.reservation_state is ReservationState.ACTIVE
        with pytest.raises(InvariantViolation) as reservation_error:
            await budget.reserve_run(reclaimed)
        assert reservation_error.value.code == "active_reservation_recovery_required"
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_run_results "
                    "SET scheduling_state = 'FAILED', lease_owner = NULL, "
                    "lease_expires_at = NULL, source_terminal_at = clock_timestamp(), "
                    "version = version + 1, updated_at = clock_timestamp() "
                    "WHERE id = :result"
                ),
                {"result": reclaimed.id},
            )
            await connection.execute(
                text(
                    "UPDATE shadow_campaigns SET failed_count = failed_count + 1, "
                    "version = version + 1, updated_at = clock_timestamp() "
                    "WHERE id = :campaign"
                ),
                {"campaign": first.id},
            )
        uncertain_usage = SettlementUsage(
            observed_provider_calls=0,
            prompt_tokens=0,
            completion_tokens=0,
            observed_estimated_cost=Decimal("0"),
            possibly_billed_call_charge=8,
            possibly_billed_cost_charge=Decimal("0.006"),
        )
        settled = await budget.settle_result(
            tenant_id,
            first.id,
            reclaimed.id,
            source_snapshot_digest="d" * 64,
            usage=uncertain_usage,
        )
        duplicate_settlement = await budget.settle_result(
            tenant_id,
            first.id,
            reclaimed.id,
            source_snapshot_digest="d" * 64,
            usage=uncertain_usage,
        )
        assert settled.duplicate is False
        assert settled.budget_violation is False
        assert duplicate_settlement.duplicate is True
        with pytest.raises(InvariantViolation) as settlement_error:
            await budget.settle_result(
                tenant_id,
                first.id,
                reclaimed.id,
                source_snapshot_digest="e" * 64,
                usage=uncertain_usage,
            )
        assert settlement_error.value.code == "settlement_conflict"
        reclaimed_fourth = await scheduler.claim_next(
            tenant_id,
            first.id,
            "worker-g",
        )
        assert reclaimed_fourth is not None
        assert reclaimed_fourth.id == fourth_lease.id
        denied_reservation = await budget.reserve_run(reclaimed_fourth)
        assert denied_reservation.allowed is False
        assert denied_reservation.stop_intent.value == "BUDGET"
        assert denied_reservation.reason == "estimated_cost_stop_threshold"
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            row = (
                await connection.execute(
                    text(
                        "SELECT status, stop_intent, stop_reason, "
                        "creation_request_hash, profile_hash, "
                        "planned_count, submitted_count, failed_count, skipped_count, "
                        "version, reserved_provider_calls, "
                        "reserved_estimated_cost, observed_provider_calls, "
                        "possibly_billed_call_charge, possibly_billed_cost_charge, "
                        "possibly_billed_count, "
                        "(SELECT count(*) FROM shadow_run_results "
                        " WHERE campaign_id = :id) AS result_count "
                        "FROM shadow_campaigns WHERE id = :id"
                    ),
                    {"id": first.id},
                )
            ).one()
        assert row == (
            "STOPPING",
            "BUDGET",
            "Budget admission denied: estimated_cost_stop_threshold",
            first.request_hash,
            DOCKER_SMOKE_V1.sha256,
            6,
            1,
            1,
            1,
            12,
            0,
            Decimal("0E-10"),
            0,
            8,
            Decimal("0.0060000000"),
            1,
            6,
        )
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            reservation_states = (
                await connection.execute(
                    text(
                        "SELECT schedule_ordinal, scheduling_state, reservation_state, "
                        "conversation_attempt_count, submission_attempt_count, "
                        "conversation_id IS NOT NULL, search_run_id IS NOT NULL "
                        "FROM shadow_run_results "
                        "WHERE campaign_id = :id AND schedule_ordinal IN (1, 2, 3, 4) "
                        "ORDER BY schedule_ordinal"
                    ),
                    {"id": first.id},
                )
            ).all()
        assert reservation_states == [
            (1, "SUBMITTED", "SETTLED", 1, 2, True, True),
            (2, "SKIPPED", "RELEASED", 0, 0, False, False),
            (3, "FAILED", "SETTLED", 0, 0, False, False),
            (4, "CLAIMED", "NONE", 0, 0, False, False),
        ]
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()
