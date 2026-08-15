"""Application orchestration for fenced two-phase shadow collection."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sana.modules.shadow_campaign.collector import (
    CollectionReceipt,
    CollectorLease,
    collect_run_snapshot,
)
from sana.modules.shadow_campaign.manifest import ShadowManifest
from sana.modules.shared.errors import InvariantViolation

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.ports import (
        CampaignSnapshotReader,
        CampaignUnitOfWorkFactory,
    )


class ShadowCollectorService:
    def __init__(
        self,
        uow_factory: "CampaignUnitOfWorkFactory",
        snapshot_reader: "CampaignSnapshotReader",
        *,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("Collector lease duration must be positive")
        self._uow_factory = uow_factory
        self._snapshot_reader = snapshot_reader
        self._lease_duration = lease_duration

    async def claim_next(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        worker_id: str,
    ) -> CollectorLease | None:
        normalized_worker = worker_id.strip()
        if not normalized_worker or len(normalized_worker) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        async with self._uow_factory(tenant_id) as uow:
            lease = await uow.campaign_collector.claim_next(
                tenant_id,
                campaign_id,
                normalized_worker,
                self._lease_duration,
            )
            if lease is None:
                return None
            await uow.commit()
            return lease

    async def renew(self, lease: CollectorLease) -> CollectorLease:
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaign_collector.renew(lease, self._lease_duration)
            await uow.commit()
        return lease

    async def collect(
        self,
        lease: CollectorLease,
        manifest: ShadowManifest,
    ) -> CollectionReceipt:
        if (
            manifest.version != lease.manifest_version
            or manifest.sha256 != lease.manifest_hash
        ):
            raise InvariantViolation(
                "Collector manifest does not match the frozen Campaign",
                code="collector_manifest_mismatch",
            )
        case = next((item for item in manifest.cases if item.id == lease.case_id), None)
        if case is None:
            raise InvariantViolation(
                "Collector case is missing from the frozen manifest",
                code="collector_case_missing",
            )
        snapshot = await self._snapshot_reader.read(lease)
        if (
            snapshot.tenant_id != lease.tenant_id
            or snapshot.run_id != lease.search_run_id
            or snapshot.conversation_id != lease.conversation_id
        ):
            raise InvariantViolation(
                "Collector reader returned a different source binding",
                code="collector_source_binding_changed",
            )
        outcome = collect_run_snapshot(
            snapshot,
            case,
            lease.cost_rate,
            oracle_version=manifest.version,
            collector_schema_version=lease.collector_schema_version,
        )
        async with self._uow_factory(lease.tenant_id) as uow:
            receipt = await uow.campaign_collector.persist(lease, outcome)
            await uow.commit()
            return receipt


__all__ = ["ShadowCollectorService"]
