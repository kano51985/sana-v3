"""Artifact payload port used between independently executed workflow steps."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from sana.modules.orchestration.domain import ArtifactRef


class ArtifactStore(Protocol):
    async def put_bytes(
        self,
        tenant_id: UUID,
        run_id: UUID,
        payload: bytes,
    ) -> ArtifactRef: ...

    async def get_bytes(self, tenant_id: UUID, reference: ArtifactRef) -> bytes: ...

    async def put_json(
        self,
        tenant_id: UUID,
        run_id: UUID,
        payload: Any,
    ) -> ArtifactRef: ...

    async def get_json(self, tenant_id: UUID, reference: ArtifactRef) -> Any: ...
