"""Ports owned by the shadow campaign domain; adapters live outside this package."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


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
