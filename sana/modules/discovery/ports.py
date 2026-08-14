"""Discovery provider contract."""

from __future__ import annotations

from typing import Protocol

from sana.modules.discovery.domain import DiscoveryQuery, ProviderResponse


class DiscoveryProvider(Protocol):
    name: str

    async def search(
        self,
        query: DiscoveryQuery,
        *,
        timeout_seconds: float,
    ) -> ProviderResponse: ...


class ProviderCircuitBreaker(Protocol):
    def allow_request(self) -> bool: ...

    def record_success(self) -> None: ...

    def record_failure(self) -> None: ...
