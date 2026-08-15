"""Ports owned by the shadow campaign domain; adapters live outside this package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sana.modules.shadow_campaign.domain import CampaignLifecycle

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from sana.modules.shadow_campaign.budget import (
        BudgetReleaseReceipt,
        BudgetReservationReceipt,
        BudgetSettlementReceipt,
        SettlementUsage,
    )
    from sana.modules.shadow_campaign.execution import (
        CampaignSubmissionReceipt,
        CandidateSubmissionReceipt,
    )
    from sana.modules.shadow_campaign.collector import (
        CollectionOutcome,
        CollectionReceipt,
        CollectorLease,
        RunSourceSnapshot,
    )
    from sana.modules.shadow_campaign.scheduler import (
        CampaignSchedulingEvidence,
        RunLease,
        RunPlan,
    )
    from sana.modules.shadow_campaign.service import (
        CampaignCreation,
        CampaignParentEvidence,
        ExistingCampaign,
    )
    from sana.modules.shadow_campaign.review import (
        ReviewProjection,
        ReviewReceipt,
        ReviewSubmission,
    )
    from sana.modules.shadow_campaign.report import (
        CampaignReportSnapshot,
        FinalReportBinding,
        FinalReportReceipt,
    )


class CampaignRepository(Protocol):
    async def find_creation(
        self,
        tenant_id: UUID,
        user_id: UUID,
        idempotency_key: str,
    ) -> ExistingCampaign | None: ...

    async def parent_evidence(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignParentEvidence | None: ...

    async def add(self, creation: CampaignCreation) -> bool: ...

    async def get_for_update(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignLifecycle | None: ...

    async def save_lifecycle(self, campaign: CampaignLifecycle) -> None: ...

    async def scheduling_evidence_for_update(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
    ) -> CampaignSchedulingEvidence | None: ...

    async def materialize_results(
        self,
        evidence: CampaignSchedulingEvidence,
        plans: tuple[RunPlan, ...],
        now: datetime,
    ) -> int: ...

    async def claim_next_result(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> RunLease | None: ...

    async def renew_result_lease(
        self,
        lease: RunLease,
        lease_duration: timedelta,
    ) -> None: ...

    async def reserve_run_budget(
        self,
        lease: RunLease,
    ) -> BudgetReservationReceipt: ...

    async def settle_run_budget(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
        source_snapshot_digest: str,
        usage: SettlementUsage,
    ) -> BudgetSettlementReceipt: ...

    async def release_run_budget(
        self,
        lease: RunLease,
    ) -> BudgetReleaseReceipt: ...


class CampaignExecutionRepository(Protocol):
    async def prepare_conversation_attempt(self, lease: RunLease) -> None: ...

    async def bind_conversation(
        self,
        lease: RunLease,
        conversation_id: UUID,
    ) -> None: ...

    async def prepare_submission_attempt(self, lease: RunLease) -> None: ...

    async def bind_submission(
        self,
        lease: RunLease,
        receipt: CandidateSubmissionReceipt,
    ) -> CampaignSubmissionReceipt: ...


class CampaignCollectorRepository(Protocol):
    async def claim_next(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> CollectorLease | None: ...

    async def renew(
        self,
        lease: CollectorLease,
        lease_duration: timedelta,
    ) -> None: ...

    async def persist(
        self,
        lease: CollectorLease,
        outcome: CollectionOutcome,
    ) -> CollectionReceipt: ...


class CampaignSnapshotReader(Protocol):
    async def read(self, lease: CollectorLease) -> RunSourceSnapshot: ...


class CampaignReviewRepository(Protocol):
    async def add(self, submission: ReviewSubmission) -> ReviewReceipt: ...


class CampaignReviewProjectionReader(Protocol):
    async def read(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
        result_id: UUID,
    ) -> ReviewProjection | None: ...


class CampaignUnitOfWork(Protocol):
    campaigns: CampaignRepository
    campaign_execution: CampaignExecutionRepository
    campaign_collector: CampaignCollectorRepository
    campaign_reviews: CampaignReviewRepository

    async def __aenter__(self) -> "CampaignUnitOfWork": ...

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None: ...

    async def commit(self) -> None: ...


class CampaignUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: UUID) -> CampaignUnitOfWork: ...


class CampaignReportStore(Protocol):
    async def put(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        payload: bytes,
        *,
        media_type: str,
    ) -> str: ...

    async def get(self, tenant_id: UUID, campaign_id: UUID, uri: str) -> bytes: ...


class CampaignReportGateway(Protocol):
    async def read(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
    ) -> CampaignReportSnapshot | None: ...

    async def bind(self, binding: FinalReportBinding) -> FinalReportReceipt: ...
