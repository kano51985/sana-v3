"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from sana.app.api.auth import DevAuthProvider, OIDCAuthProvider
from sana.app.api.dependencies import AppContainer
from sana.app.api.routes import conversations, events, runs
from sana.app.api.services import DatabaseRunApplicationService, DatabaseRunEventService
from sana.app.settings import SanaSettings
from sana.modules.conversation.domain import ConversationService
from sana.modules.orchestration.policy import SearchPolicy
from sana.modules.orchestration.routing import AutomaticModeRouter
from sana.modules.shared.clock import SystemClock
from sana.modules.shared.errors import TypedError
from sana.modules.shared.ids import RandomIdFactory
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory
from sana.platform.events.redis_stream import RedisEventStream


def _build_default_container(settings: SanaSettings):
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    uow_factory = TenantUnitOfWorkFactory(session_factory)
    clock = SystemClock()
    policy = SearchPolicy.default()
    if settings.auth_mode == "oidc":
        auth_provider = OIDCAuthProvider(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=settings.oidc_jwks_url,
            tenant_claim=settings.oidc_tenant_claim,
            user_claim=settings.oidc_user_claim,
        )
    else:
        auth_provider = DevAuthProvider()
    redis = Redis.from_url(settings.redis_url)
    redis_stream = RedisEventStream(redis)
    id_factory = RandomIdFactory()
    container = AppContainer(
        auth_provider=auth_provider,
        conversation_service=ConversationService(
            uow_factory,
            id_factory,
            clock,
            policy,
        ),
        router=AutomaticModeRouter(policy.version),
        run_service=DatabaseRunApplicationService(
            uow_factory,
            clock,
            id_factory,
            redis_stream,
        ),
        event_service=DatabaseRunEventService(
            uow_factory,
            redis_stream,
            block_ms=settings.sse_block_ms,
        ),
    )
    return container, engine, redis


def create_app(
    container: AppContainer | None = None,
    settings: SanaSettings | None = None,
) -> FastAPI:
    owned_resources = None
    if container is None:
        resolved_settings = settings or SanaSettings()
        container, engine, redis = _build_default_container(resolved_settings)
        owned_resources = (engine, redis)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        yield
        if owned_resources is not None:
            engine, redis = owned_resources
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Sana API",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(conversations.router)
    app.include_router(runs.router)
    app.include_router(events.router)

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(TypedError)
    async def typed_error_handler(request: Request, exc: TypedError) -> JSONResponse:
        del request
        http_status = (
            status.HTTP_404_NOT_FOUND
            if exc.code.endswith("not_found")
            else status.HTTP_409_CONFLICT
        )
        return JSONResponse(status_code=http_status, content={"detail": exc.message})

    return app


app = create_app()
