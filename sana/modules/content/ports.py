"""Content fetch, capability and navigation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sana.modules.content.domain import (
    FetchArtifact,
    FetchRequest,
    ReusableContentSnapshot,
)


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    name: str
    available: bool
    detail: str = ""


class ContentFetcher(Protocol):
    async def fetch(self, request: FetchRequest) -> FetchArtifact: ...


class ContentSnapshotReader(Protocol):
    async def latest_for_url(
        self,
        tenant_id: UUID,
        canonical_url_hash: str,
    ) -> ReusableContentSnapshot | None: ...


class URLSafetyValidator(Protocol):
    async def validate(self, url: str) -> None: ...


class CapabilityProbe(Protocol):
    async def probe(self) -> CapabilityStatus: ...


class NavigationProvider(Protocol):
    async def discover_links(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]: ...
