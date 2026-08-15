from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from sana.app.api.services import DatabaseConversationCatalogService
from sana.app.shadow_collector import ShadowCollectorService
from sana.modules.conversation.domain import ConversationService, SubmitMessageCommand
from sana.modules.identity.domain import Principal
from sana.modules.orchestration.domain import RoutingDecision, SearchMode
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.shadow_campaign.budget import CampaignBudgetService
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ErrorClass,
    StopIntent,
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
from sana.modules.shadow_campaign.runner import RunnerFailure
from sana.modules.shadow_campaign.scheduler import CampaignSchedulingService
from sana.modules.shadow_campaign.service import (
    CampaignLifecycleService,
    CampaignProvenance,
    CampaignService,
    CreateCampaignCommand,
)
from sana.modules.shared.clock import FrozenClock
from sana.modules.shared.ids import (
    DeterministicIdFactory,
    RandomIdFactory,
    TraceContext,
)
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory


DATABASE_URL = os.environ.get("SANA_TEST_DATABASE_URL")
NOW = datetime(2026, 8, 15, tzinfo=UTC)


class InProcessCandidate:
    def __init__(self, principal, catalog, conversations, policy_version) -> None:
        self._principal = principal
        self._catalog = catalog
        self._conversations = conversations
        self._policy_version = policy_version

    async def create_conversation(self, *, title: str, idempotency_key: str):
        return (
            await self._catalog.create(self._principal, title, idempotency_key)
        ).id

    async def submit_message(
        self,
        *,
        conversation_id,
        content: str,
        idempotency_key: str,
    ) -> CandidateSubmissionReceipt:
        receipt = await self._conversations.submit_message(
            SubmitMessageCommand(
                self._principal.tenant_id,
                self._principal.user_id,
                conversation_id,
                content,
                idempotency_key,
                RoutingDecision(
                    SearchMode.FAST,
                    ("shadow_runner_test",),
                    self._policy_version,
                    1.0,
                ),
                TraceContext.create(),
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
async def test_stopping_recovery_and_unknown_failure_close_the_ledger() -> None:
    engine = create_database_engine(DATABASE_URL)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    tenant_id, user_id, other_user_id = uuid4(), uuid4(), uuid4()
    environment = {"compose_project": "runner-test", "network": "isolated"}
    manifest = ShadowManifest(
        "shadow-cases-v1",
        tuple(
            SimpleNamespace(
                id=f"case-{index:02d}",
                prompt=f"runner prompt {index}",
                expected_mode=SearchMode.FAST if index < 3 else SearchMode.RESEARCH,
                locale="zh-CN" if index % 2 == 0 else "en",
                category=CaseCategory.VERSION,
                answerability=Answerability.ANSWERABLE,
                smoke=index < 6,
            )
            for index in range(40)
        ),
        "c" * 64,
    )
    rubric = ReviewRubric("review-v1")
    rate = CostRate("rate-v1", Decimal("0.1"), Decimal("0.2"), Decimal("0.001"))
    catalog = CampaignPolicyCatalog.standard(
        review_rubrics=(rubric,),
        cost_rates=(rate,),
    )
    command = CreateCampaignCommand(
        tenant_id,
        user_id,
        "runner integration",
        "runner-integration-key",
        DOCKER_SMOKE_V1.version,
        manifest,
        rubric,
        rate,
        CampaignProvenance(
            "a" * 40,
            True,
            f"candidate@sha256:{'a' * 64}",
            "a" * 40,
            "0012_fetch_run_binding",
            "a" * 64,
            "b" * 40,
            True,
            "b" * 64,
            "shadow-collector-v2",
            snapshot_hash(environment),
            environment,
        ),
        NOW + timedelta(days=30),
    )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, status) "
                    "VALUES (:id, :slug, 'Runner Test', 'ACTIVE')"
                ),
                {"id": tenant_id, "slug": f"runner-{tenant_id}"},
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            for identity in (user_id, other_user_id):
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, tenant_id, email, display_name, status) "
                        "VALUES (:id, :tenant, :email, 'Runner User', 'ACTIVE')"
                    ),
                    {
                        "id": identity,
                        "tenant": tenant_id,
                        "email": f"{identity}@example.test",
                    },
                )

        clock = FrozenClock(NOW)
        campaigns = CampaignService(
            uow_factory,
            DeterministicIdFactory("runner-integration"),
            clock,
            catalog,
        )
        receipt = await campaigns.create(command)
        scheduler = CampaignSchedulingService(uow_factory, clock, catalog)
        await scheduler.materialize(tenant_id, user_id, receipt.id, manifest)
        lifecycle = CampaignLifecycleService(uow_factory, clock)
        await lifecycle.start(tenant_id, user_id, receipt.id)
        lease = await scheduler.claim_next(tenant_id, receipt.id, "runner-first")
        submitted_lease = await scheduler.claim_next(
            tenant_id,
            receipt.id,
            "runner-submitted",
        )
        assert lease is not None and lease.schedule_ordinal == 1
        assert submitted_lease is not None and submitted_lease.schedule_ordinal == 2
        budget = CampaignBudgetService(uow_factory)
        assert (await budget.reserve_run(lease)).allowed
        assert (await budget.reserve_run(submitted_lease)).allowed
        principal = Principal(tenant_id, user_id, "integration", str(user_id))
        search_policy = SearchPolicy.default()
        execution = CampaignExecutionService(
            uow_factory,
            InProcessCandidate(
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
            ),
        )
        submission = await execution.execute(
            submitted_lease,
            next(item.prompt for item in manifest.cases if item.id == submitted_lease.case_id),
        )
        async with uow_factory(tenant_id) as uow:
            await uow.campaign_execution.prepare_conversation_attempt(lease)
            await uow.commit()
        await lifecycle.request_stop(
            tenant_id,
            user_id,
            receipt.id,
            StopIntent.FATAL,
            "candidate_api_unavailable",
        )
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
                {"result": lease.id},
            )
            await connection.execute(
                text(
                    "UPDATE search_runs SET status = 'FAILED', "
                    "stop_reason = 'RUNNER_TEST', completed_at = clock_timestamp() "
                    "WHERE id = :run"
                ),
                {"run": submission.search_run_id},
            )

        recovered = await scheduler.claim_next(tenant_id, receipt.id, "runner-recovery")
        assert recovered is not None and recovered.id == lease.id
        assert await scheduler.claim_next(tenant_id, receipt.id, "no-new-work") is None
        collector = ShadowCollectorService(uow_factory, None)  # type: ignore[arg-type]
        collector_lease = await collector.claim_next(
            tenant_id,
            receipt.id,
            "collector-failure",
        )
        assert collector_lease is not None
        async with uow_factory(tenant_id) as uow:
            collector_failure = await uow.campaign_runner.mark_collector_failure(
                collector_lease,
                RunnerFailure(
                    ErrorClass.PERMANENT_CONFIGURATION,
                    "source_topology_invalid",
                    "collector_validation",
                    False,
                ),
            )
            await uow.commit()
        assert not collector_failure.duplicate
        async with uow_factory(tenant_id) as uow:
            failure = await uow.campaign_runner.mark_failure(
                recovered,
                RunnerFailure(
                    ErrorClass.PROVIDER_TRANSIENT,
                    "transport_exhausted",
                    "candidate_api",
                    True,
                ),
            )
            await uow.commit()
        assert failure.possibly_billed and not failure.duplicate

        async with uow_factory(tenant_id) as uow:
            skipped = await uow.campaign_runner.skip_pending(
                tenant_id,
                receipt.id,
                "campaign_fatal",
            )
            await uow.commit()
        assert skipped == 4
        settled = await lifecycle.settle_stop(tenant_id, receipt.id)
        assert settled is not None and settled.status is CampaignStatus.ABORTED

        async with uow_factory(tenant_id) as uow:
            owner_state = await uow.campaign_runner.read_owned_state(
                tenant_id,
                user_id,
                receipt.id,
            )
            hidden = await uow.campaign_runner.read_owned_state(
                tenant_id,
                other_user_id,
                receipt.id,
            )
        assert hidden is None
        assert owner_state is not None
        assert owner_state.failed_count == 2
        assert owner_state.skipped_count == 4
        assert owner_state.active_reservation_count == 0
        assert owner_state.execution_sealed

        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant, true)"),
                {"tenant": str(tenant_id)},
            )
            ledger = (
                await connection.execute(
                    text(
                        "SELECT status, failed_count, skipped_count, "
                        "reserved_provider_calls, reserved_estimated_cost, "
                        "possibly_billed_call_charge, possibly_billed_cost_charge, "
                        "possibly_billed_count "
                        "FROM shadow_campaigns WHERE id = :campaign"
                    ),
                    {"campaign": receipt.id},
                )
            ).one()
        assert ledger == (
            "ABORTED",
            2,
            4,
            0,
            Decimal("0E-10"),
            8,
            Decimal("0.0010000000"),
            1,
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM tenants WHERE id = :tenant"),
                {"tenant": tenant_id},
            )
        await engine.dispose()
