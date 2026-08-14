"""Content fetch, capability and navigation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sana.modules.content.domain import FetchArtifact, FetchRequest


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    name: str
    available: bool
    detail: str = ""


class ContentFetcher(Protocol):
    async def fetch(self, request: FetchRequest) -> FetchArtifact: ...


class CapabilityProbe(Protocol):
    async def probe(self) -> CapabilityStatus: ...


class NavigationProvider(Protocol):
    async def discover_links(
        self,
        url: str,
        *,
        timeout_seconds: float,
    ) -> tuple[str, ...]: ...
