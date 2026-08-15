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
from sana.modules.answer.model_synthesizer import ConstrainedModelSynthesizer
from sana.modules.discovery.service import DiscoveryService
from sana.modules.evidence.model_verifier import ModelEvidenceVerifier
from sana.modules.model_gateway.domain import ModelRole, OutputFormat, ThinkingMode
from sana.modules.model_gateway.service import ModelGateway, RoleConfig
from sana.modules.orchestration.executor import DurableStepExecutor
from sana.modules.orchestration.lease import LeaseService
from sana.modules.orchestration.step_handlers import build_fast_handler_registry
from sana.modules.search_planning.planner import SearchPlanner
from sana.modules.shared.clock import SystemClock
from sana.modules.shared.ids import RandomIdFactory, TraceContext
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.model_audit import SqlModelInvocationAuditSink
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.events.redis_stream import RedisEventStream
from sana.platform.fetch.http_fetcher import HttpContentFetcher
from sana.platform.models.deepseek import DeepSeekModelProvider
from sana.platform.models.local import LocalModelProvider
from sana.platform.models.openai import OpenAIModelProvider
from sana.platform.search.bing_rss import BingRssProvider
from sana.platform.search.direct_source import DirectSourceProvider
from sana.platform.search.circuit_breaker import CircuitBreaker
from sana.platform.search.searxng import SearxngProvider
from sana.platform.security.secrets import EnvironmentSecretProvider
from sana.platform.security.ssrf import SSRFGuard
from sana.platform.storage.local_artifacts import LocalArtifactStore


class ProductionWorkerSettings(SanaSettings):
    worker_model_pipeline_enabled: bool = False
    worker_planner_provider: Literal[
        "heuristic", "deepseek", "openai", "local"
    ] = "deepseek"
    worker_planner_model: str = "deepseek-v4-flash"
    worker_verifier_provider: Literal["deepseek", "openai", "local"] = "deepseek"
    worker_verifier_model: str = "deepseek-v4-flash"
    worker_synthesizer_provider: Literal["deepseek", "openai", "local"] = "deepseek"
    worker_synthesizer_model: str = "deepseek-v4-flash"
    worker_deepseek_base_url: str = "https://api.deepseek.com"
    worker_model_thinking: Literal["disabled"] = "disabled"
    worker_model_output_format: Literal["json_object"] = "json_object"
    worker_live_eval_max_runs: int = 20
    worker_discovery_providers: str = "direct,bing_rss"
    worker_searxng_url: str = ""
    worker_max_selected_hits: int = 4
    worker_heartbeat_seconds: float = 2.0
    worker_lease_seconds: float = 6.0

    @model_validator(mode="after")
    def validate_worker(self) -> "ProductionWorkerSettings":
        if self.worker_model_pipeline_enabled:
            roles = (
                ("planner", self.worker_planner_provider, self.worker_planner_model),
                ("verifier", self.worker_verifier_provider, self.worker_verifier_model),
                (
                    "synthesizer",
                    self.worker_synthesizer_provider,
                    self.worker_synthesizer_model,
                ),
            )
            for role, provider, model in roles:
                if provider == "heuristic" or not model.strip():
                    raise ValueError(
                        f"Enabled model pipeline requires a configured {role} model"
                    )
            configured_providers = {provider for _, provider, _ in roles}
            if len(configured_providers) != 1:
                raise ValueError(
                    "Enabled model pipeline requires one shared provider for all roles"
                )
        if not self.worker_deepseek_base_url.strip():
            raise ValueError("DeepSeek base URL cannot be empty")
        if not 1 <= self.worker_live_eval_max_runs <= 20:
            raise ValueError("Live eval run limit must be between 1 and 20")
        providers = self.discovery_provider_names
        unsupported = set(providers) - {"direct", "bing_rss", "searxng"}
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


def _model_stack(
    settings: ProductionWorkerSettings,
    clock: SystemClock,
    uow_factory: TenantUnitOfWorkFactory,
    artifacts: LocalArtifactStore,
    ids: RandomIdFactory,
):
    if (
        not settings.worker_model_pipeline_enabled
        or settings.worker_planner_provider == "heuristic"
    ):
        from sana.modules.orchestration.policy import SearchPolicy

        return (
            HeuristicIntentPlanner(SearchPolicy.default().version),
            None,
            None,
            None,
        )
    secrets = EnvironmentSecretProvider()
    provider_name = settings.worker_planner_provider
    if provider_name == "deepseek" and not secrets.get_secret("DEEPSEEK_API_KEY"):
        raise ValueError("Enabled DeepSeek model pipeline requires a Worker credential")
    if provider_name == "deepseek":
        provider = DeepSeekModelProvider(
            secrets,
            base_url=settings.worker_deepseek_base_url,
        )
    elif provider_name == "openai":
        if not secrets.get_secret("OPENAI_API_KEY"):
            raise ValueError("Enabled OpenAI model pipeline requires a Worker credential")
        provider = OpenAIModelProvider(secrets)
    else:
        provider = LocalModelProvider()
    audit = SqlModelInvocationAuditSink(uow_factory, artifacts, clock, ids)
    gateway = ModelGateway(
        {provider_name: provider},
        {
            ModelRole.PLANNER: RoleConfig(
                provider_name,
                settings.worker_planner_model,
                temperature=0.0,
                max_output_tokens=1_024,
                max_retries=1,
                request_timeout_seconds=30.0,
                output_format=OutputFormat(settings.worker_model_output_format),
                thinking_mode=ThinkingMode(settings.worker_model_thinking),
                prompt_template_version="planner-v1",
                parser_schema_version="search-intent-v1",
            ),
            ModelRole.VERIFIER: RoleConfig(
                provider_name,
                settings.worker_verifier_model,
                temperature=0.0,
                max_output_tokens=2_048,
                max_retries=1,
                request_timeout_seconds=30.0,
                output_format=OutputFormat(settings.worker_model_output_format),
                thinking_mode=ThinkingMode(settings.worker_model_thinking),
                prompt_template_version="verifier-v1",
                parser_schema_version="evidence-verdicts-v1",
            ),
            ModelRole.SYNTHESIZER: RoleConfig(
                provider_name,
                settings.worker_synthesizer_model,
                temperature=0.0,
                max_output_tokens=2_048,
                max_retries=1,
                request_timeout_seconds=30.0,
                output_format=OutputFormat(settings.worker_model_output_format),
                thinking_mode=ThinkingMode(settings.worker_model_thinking),
                prompt_template_version="synthesizer-v1",
                parser_schema_version="proposed-claims-v1",
            ),
        },
        clock,
        audit,
    )
    return (
        ModelIntentPlanner(SearchPlanner(gateway)),
        ModelEvidenceVerifier(gateway),
        ConstrainedModelSynthesizer(gateway),
        provider,
    )


async def build_worker_runtime(
    settings: ProductionWorkerSettings,
) -> WorkerRuntime:
    clock = SystemClock()
    ids = RandomIdFactory()
    engine = create_database_engine(settings.database_url)
    sessions = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(sessions)
    artifacts = LocalArtifactStore(settings.artifact_root)
    planner, model_verifier, model_synthesizer, model_provider = _model_stack(
        settings,
        clock,
        uow_factory,
        artifacts,
        ids,
    )
    redis = Redis.from_url(settings.redis_url)
    event_stream = RedisEventStream(redis)
    discovery_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    providers = {}
    for name in settings.discovery_provider_names:
        if name == "direct":
            providers[name] = DirectSourceProvider()
        elif name == "bing_rss":
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
    operations = SearchStepOperations(
        uow_factory,
        artifacts,
        planner,
        discovery,
        fetcher,
        settings.discovery_provider_names,
        settings.worker_max_selected_hits,
        model_verifier,
        model_synthesizer,
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
