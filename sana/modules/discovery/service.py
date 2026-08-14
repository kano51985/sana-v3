"""Bounded concurrent discovery with independent provider responses."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from time import perf_counter
from typing import AsyncIterator
from uuid import UUID

from sana.modules.discovery.domain import (
    DiscoveryQuery,
    ProviderMetrics,
    ProviderResponse,
)
from sana.modules.discovery.ports import DiscoveryProvider, ProviderCircuitBreaker
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import ErrorCategory, TypedError


class DiscoveryConcurrency:
    def __init__(
        self,
        *,
        global_limit: int = 20,
        tenant_limit: int = 6,
        provider_limit: int = 4,
    ) -> None:
        if min(global_limit, tenant_limit, provider_limit) < 1:
            raise ValueError("Discovery concurrency limits must be positive")
        self._global = asyncio.Semaphore(global_limit)
        self._tenant_limit = tenant_limit
        self._provider_limit = provider_limit
        self._tenants: dict[UUID, asyncio.Semaphore] = {}
        self._tenant_references: dict[UUID, int] = {}
        self._providers: dict[str, asyncio.Semaphore] = {}
        self._registry_lock = asyncio.Lock()

    async def _semaphores(
        self,
        tenant_id: UUID,
        provider: str,
    ) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
        async with self._registry_lock:
            tenant = self._tenants.setdefault(
                tenant_id,
                asyncio.Semaphore(self._tenant_limit),
            )
            provider_gate = self._providers.setdefault(
                provider,
                asyncio.Semaphore(self._provider_limit),
            )
            self._tenant_references[tenant_id] = (
                self._tenant_references.get(tenant_id, 0) + 1
            )
            return tenant, provider_gate

    async def _release_tenant_reference(self, tenant_id: UUID) -> None:
        async with self._registry_lock:
            remaining = self._tenant_references[tenant_id] - 1
            if remaining == 0:
                del self._tenant_references[tenant_id]
                del self._tenants[tenant_id]
            else:
                self._tenant_references[tenant_id] = remaining

    @asynccontextmanager
    async def acquire(self, tenant_id: UUID, provider: str) -> AsyncIterator[None]:
        tenant, provider_gate = await self._semaphores(tenant_id, provider)
        try:
            async with tenant:
                async with provider_gate:
                    async with self._global:
                        yield
        finally:
            await self._release_tenant_reference(tenant_id)


class DiscoveryService:
    def __init__(
        self,
        providers: dict[str, DiscoveryProvider],
        clock: Clock,
        *,
        concurrency: DiscoveryConcurrency | None = None,
        breakers: dict[str, ProviderCircuitBreaker] | None = None,
    ) -> None:
        self._providers = dict(providers)
        self._clock = clock
        self._concurrency = concurrency or DiscoveryConcurrency()
        self._breakers = dict(breakers or {})

    def _remaining(self, deadline: datetime) -> float:
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("Discovery deadline must be timezone-aware")
        return (deadline - self._clock.now()).total_seconds()

    async def _search_one(
        self,
        tenant_id: UUID,
        provider: DiscoveryProvider,
        query: DiscoveryQuery,
        deadline: datetime,
    ) -> ProviderResponse:
        breaker = self._breakers.get(provider.name)
        started = perf_counter()
        async with self._concurrency.acquire(tenant_id, provider.name):
            remaining = self._remaining(deadline)
            if remaining <= 0:
                return ProviderResponse(
                    provider.name,
                    query.key,
                    (),
                    ProviderMetrics(int((perf_counter() - started) * 1000)),
                    TypedError(
                        ErrorCategory.BUDGET,
                        "discovery_deadline_exceeded",
                        "Discovery deadline was exhausted before provider call",
                        retryable=False,
                    ),
                )
            if breaker is not None and not breaker.allow_request():
                return ProviderResponse(
                    provider.name,
                    query.key,
                    (),
                    ProviderMetrics(int((perf_counter() - started) * 1000)),
                    TypedError(
                        ErrorCategory.TRANSIENT,
                        "provider_circuit_open",
                        f"Provider circuit is open: {provider.name}",
                        retryable=True,
                    ),
                )
            try:
                async with asyncio.timeout(remaining):
                    response = await provider.search(
                        query,
                        timeout_seconds=remaining,
                    )
            except TimeoutError as exc:
                response = ProviderResponse(
                    provider.name,
                    query.key,
                    (),
                    ProviderMetrics(int((perf_counter() - started) * 1000)),
                    TypedError(
                        ErrorCategory.TRANSIENT,
                        "provider_timeout",
                        f"Provider timed out: {provider.name}",
                        retryable=True,
                        cause=exc,
                    ),
                )
            except TypedError as exc:
                response = ProviderResponse(
                    provider.name,
                    query.key,
                    (),
                    ProviderMetrics(int((perf_counter() - started) * 1000)),
                    exc,
                )
            except Exception as exc:
                response = ProviderResponse(
                    provider.name,
                    query.key,
                    (),
                    ProviderMetrics(int((perf_counter() - started) * 1000)),
                    TypedError(
                        ErrorCategory.INTERNAL,
                        "provider_unexpected_error",
                        f"Provider failed unexpectedly: {provider.name}",
                        retryable=False,
                        cause=exc,
                    ),
                )
        if breaker is not None:
            if response.ok:
                breaker.record_success()
            elif response.error is not None and response.error.category in {
                ErrorCategory.TRANSIENT,
                ErrorCategory.INTERNAL,
            }:
                breaker.record_failure()
        return response

    async def discover(
        self,
        tenant_id: UUID,
        queries: tuple[DiscoveryQuery, ...],
        provider_names: tuple[str, ...],
        *,
        deadline: datetime,
    ) -> tuple[ProviderResponse, ...]:
        selected = []
        for name in provider_names:
            if name not in self._providers:
                raise ValueError(f"Unknown discovery provider: {name}")
            selected.append(self._providers[name])
        tasks = [
            asyncio.create_task(self._search_one(tenant_id, provider, query, deadline))
            for query in queries
            for provider in selected
        ]
        if not tasks:
            return ()
        return tuple(await asyncio.gather(*tasks))
