"""Ports owned by the shadow campaign domain; adapters live outside this package."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from sana.modules.shadow_campaign.domain import CampaignLifecycle

if TYPE_CHECKING:
    from sana.modules.shadow_campaign.service import (
        CampaignCreation,
        CampaignParentEvidence,
        ExistingCampaign,
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


class CampaignUnitOfWork(Protocol):
    campaigns: CampaignRepository

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
