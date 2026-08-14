"""Deployment-ready Step handler composition root.

Celery is synchronous while the platform adapters are asynchronous.  Each
prefork child therefore owns one persistent event loop and creates all async
resources lazily inside that process.  This avoids reusing SQLAlchemy, Redis or
HTTP pools across forks or across a succession of ``asyncio.run`` loops.
"""

from __future__ import annotations

import atexit
import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
import os
import socket
from threading import Lock, Thread
from typing import Literal

import httpx
from pydantic import model_validator
from redis.asyncio import Redis

from sana.app.search_operations import (
    HeuristicIntentPlanner,
    ModelIntentPlanner,
    SearchStepOperations,
)
from sana.app.settings import SanaSettings
from sana.app.sql_step_execution import RedisEventMirror, SqlStepExecutionStore
from sana.app.workflow_completion import WorkflowCompletionCoordinator
from sana.modules.discovery.service import DiscoveryService
from sana.modules.model_gateway.domain import ModelRole
from sana.modules.model_gateway.service import ModelGateway, RoleConfig
from sana.modules.orchestration.executor import DurableStepExecutor
from sana.modules.orchestration.lease import LeaseService
from sana.modules.orchestration.step_handlers import build_fast_handler_registry
from sana.modules.search_planning.planner import SearchPlanner
from sana.modules.shared.clock import SystemClock
from sana.modules.shared.ids import RandomIdFactory, TraceContext
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.events.redis_stream import RedisEventStream
from sana.platform.fetch.http_fetcher import HttpContentFetcher
from sana.platform.models.deepseek import DeepSeekModelProvider
from sana.platform.models.local import LocalModelProvider
from sana.platform.models.openai import OpenAIModelProvider
from sana.platform.search.bing_rss import BingRssProvider
from sana.platform.search.circuit_breaker import CircuitBreaker
from sana.platform.search.searxng import SearxngProvider
from sana.platform.security.secrets import EnvironmentSecretProvider
from sana.platform.security.ssrf import SSRFGuard
from sana.platform.storage.local_artifacts import LocalArtifactStore


class ProductionWorkerSettings(SanaSettings):
    worker_planner_provider: Literal[
        "heuristic", "deepseek", "openai", "local"
    ] = "heuristic"
    worker_planner_model: str = ""
    worker_discovery_providers: str = "bing_rss"
    worker_searxng_url: str = ""
    worker_max_selected_hits: int = 4
    worker_heartbeat_seconds: float = 2.0
    worker_lease_seconds: float = 6.0

    @model_validator(mode="after")
    def validate_worker(self) -> "ProductionWorkerSettings":
        if self.environment == "production" and self.worker_planner_provider == "heuristic":
            raise ValueError("Production Worker requires a model-backed planner")
        if (
            self.worker_planner_provider != "heuristic"
            and not self.worker_planner_model.strip()
        ):
            raise ValueError("Model-backed Worker planner requires a model name")
        providers = self.discovery_provider_names
        unsupported = set(providers) - {"bing_rss", "searxng"}
        if unsupported:
            raise ValueError(f"Unsupported Worker discovery providers: {sorted(unsupported)}")
        if "searxng" in providers and not self.worker_searxng_url.strip():
            raise ValueError("SearXNG provider requires SANA_WORKER_SEARXNG_URL")
        if self.worker_max_selected_hits < 1:
            raise ValueError("Worker selected-hit limit must be positive")
        if self.worker_heartbeat_seconds <= 0 or self.worker_lease_seconds <= 0:
            raise ValueError("Worker heartbeat and lease must be positive")
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("Worker heartbeat must be shorter than its lease")
        return self

    @property
    def discovery_provider_names(self) -> tuple[str, ...]:
        values = tuple(
            dict.fromkeys(
                part.strip().lower()
                for part in self.worker_discovery_providers.split(",")
                if part.strip()
            )
        )
        if not values:
            raise ValueError("Worker must configure at least one discovery provider")
        return values


@dataclass(slots=True)
class WorkerRuntime:
    executor: DurableStepExecutor
    engine: object
    redis: Redis
    discovery_client: httpx.AsyncClient
    fetcher: HttpContentFetcher
    model_provider: object | None = None

    async def aclose(self) -> None:
        if self.model_provider is not None and hasattr(self.model_provider, "aclose"):
            await self.model_provider.aclose()
        await self.fetcher.aclose()
        await self.discovery_client.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def _planner(settings: ProductionWorkerSettings, clock: SystemClock):
    if settings.worker_planner_provider == "heuristic":
        from sana.modules.orchestration.policy import SearchPolicy

        return HeuristicIntentPlanner(SearchPolicy.default().version), None
    secrets = EnvironmentSecretProvider()
    if settings.worker_planner_provider == "deepseek":
        provider = DeepSeekModelProvider(secrets)
    elif settings.worker_planner_provider == "openai":
        provider = OpenAIModelProvider(secrets)
    else:
        provider = LocalModelProvider()
    gateway = ModelGateway(
        {settings.worker_planner_provider: provider},
        {
            ModelRole.PLANNER: RoleConfig(
                settings.worker_planner_provider,
                settings.worker_planner_model,
                temperature=0.0,
                max_output_tokens=2_048,
                max_retries=1,
                request_timeout_seconds=30.0,
            )
        },
        clock,
    )
    return ModelIntentPlanner(SearchPlanner(gateway)), provider


async def build_worker_runtime(
    settings: ProductionWorkerSettings,
) -> WorkerRuntime:
    clock = SystemClock()
    ids = RandomIdFactory()
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    redis = Redis.from_url(settings.redis_url)
    event_stream = RedisEventStream(redis)
    artifacts = LocalArtifactStore(settings.artifact_root)
    discovery_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    providers = {}
    for name in settings.discovery_provider_names:
        if name == "bing_rss":
            providers[name] = BingRssProvider(discovery_client)
        elif name == "searxng":
            providers[name] = SearxngProvider(
                discovery_client,
                base_url=settings.worker_searxng_url,
            )
    discovery = DiscoveryService(
        providers,
        clock,
        breakers={name: CircuitBreaker(clock) for name in providers},
    )
    fetcher = HttpContentFetcher(SSRFGuard(), clock)
    planner, model_provider = _planner(settings, clock)
    operations = SearchStepOperations(
        uow_factory,
        artifacts,
        planner,
        discovery,
        fetcher,
        settings.discovery_provider_names,
        settings.worker_max_selected_hits,
    )
    completion = WorkflowCompletionCoordinator(artifacts, clock, ids)
    store = SqlStepExecutionStore(
        uow_factory,
        LeaseService(ids, lease_seconds=settings.worker_lease_seconds),
        completion,
        clock,
        ids,
        event_mirror=RedisEventMirror(event_stream),
        lease_extension_seconds=settings.worker_lease_seconds,
    )
    executor = DurableStepExecutor(
        store,
        build_fast_handler_registry(operations.registry_operations()),
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
        heartbeat_seconds=settings.worker_heartbeat_seconds,
    )
    return WorkerRuntime(
        executor,
        engine,
        redis,
        discovery_client,
        fetcher,
        model_provider,
    )


class _PersistentAsyncRuntime:
    def __init__(self, settings: ProductionWorkerSettings) -> None:
        self._settings = settings
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(
            target=self._run_loop,
            name="sana-worker-async-runtime",
            daemon=True,
        )
        self._runtime: WorkerRuntime | None = None
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()

    async def _execute(
        self,
        tenant_id,
        step_id,
        trace_context: TraceContext,
    ) -> str:
        if self._runtime is None:
            self._runtime = await build_worker_runtime(self._settings)
        return await self._runtime.executor(tenant_id, step_id, trace_context)

    def execute(self, tenant_id, step_id, trace_context: TraceContext) -> str:
        future: Future[str] = asyncio.run_coroutine_threadsafe(
            self._execute(tenant_id, step_id, trace_context),
            self._loop,
        )
        return future.result()

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        if self._runtime is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._runtime.aclose(),
                self._loop,
            )
            future.result(timeout=15)
            self._runtime = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=15)


class ProductionStepHandler:
    """PID-aware synchronous callable consumed by the thin Celery task."""

    def __init__(self, settings: ProductionWorkerSettings) -> None:
        self._settings = settings
        self._pid: int | None = None
        self._runtime: _PersistentAsyncRuntime | None = None
        self._lock = Lock()

    def _current_runtime(self) -> _PersistentAsyncRuntime:
        pid = os.getpid()
        with self._lock:
            if self._runtime is None or self._pid != pid:
                if self._runtime is not None:
                    self._runtime.close()
                self._runtime = _PersistentAsyncRuntime(self._settings)
                self._pid = pid
            return self._runtime

    def __call__(self, tenant_id, step_id, trace_context: TraceContext) -> str:
        return self._current_runtime().execute(tenant_id, step_id, trace_context)

    def close(self) -> None:
        with self._lock:
            if self._runtime is not None:
                self._runtime.close()
                self._runtime = None
                self._pid = None


def create_handler() -> ProductionStepHandler:
    handler = ProductionStepHandler(ProductionWorkerSettings())
    atexit.register(handler.close)
    return handler


__all__ = [
    "ProductionStepHandler",
    "ProductionWorkerSettings",
    "WorkerRuntime",
    "build_worker_runtime",
    "create_handler",
]
