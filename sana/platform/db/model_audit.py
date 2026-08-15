"""PostgreSQL-backed model-call reservation, audit, and safe response reuse."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from sana.modules.model_gateway.domain import (
    BillingDisposition,
    ModelBudgetExceeded,
    ModelInvocationContext,
    ModelInvocationReservation,
    ModelInvocationStatus,
    ModelRequest,
    ProviderResponse,
    RedactedInvocationError,
    ReusedModelResponse,
)
from sana.modules.orchestration.artifact_store import ArtifactStore
from sana.modules.orchestration.domain import ArtifactRef, StepStatus
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.modules.shared.ids import IdFactory
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.orchestration import (
    SearchRunRecord,
    SearchStepRecord,
    StepAttemptRecord,
)
from sana.platform.db.uow import TenantUnitOfWork, TenantUnitOfWorkFactory


class SqlModelInvocationAuditSink:
    """Makes the database reservation the authority for actual provider calls."""

    def __init__(
        self,
        uow_factory: TenantUnitOfWorkFactory,
        artifacts: ArtifactStore,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifacts = artifacts
        self._clock = clock
        self._ids = ids

    @staticmethod
    def _reject(code: str, message: str) -> TypedError:
        return TypedError(
            ErrorCategory.PERMANENT,
            code,
            message,
            retryable=False,
        )

    async def _lock_current(
        self,
        uow: TenantUnitOfWork,
        context: ModelInvocationContext,
        deadline: datetime,
    ) -> SearchRunRecord:
        now = self._clock.now()
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("Model invocation deadline must be timezone-aware")
        if now >= deadline:
            raise TypedError(
                ErrorCategory.BUDGET,
                "model_deadline_exceeded",
                "Model call cannot start after the execution deadline",
                retryable=False,
            )
        run = await uow.session.scalar(
            select(SearchRunRecord)
            .where(
                SearchRunRecord.tenant_id == context.tenant_id,
                SearchRunRecord.id == context.run_id,
            )
            .with_for_update()
        )
        step = await uow.session.scalar(
            select(SearchStepRecord)
            .where(
                SearchStepRecord.tenant_id == context.tenant_id,
                SearchStepRecord.id == context.step_id,
                SearchStepRecord.run_id == context.run_id,
            )
            .with_for_update()
        )
        attempt = await uow.session.scalar(
            select(StepAttemptRecord)
            .where(
                StepAttemptRecord.tenant_id == context.tenant_id,
                StepAttemptRecord.id == context.attempt_id,
                StepAttemptRecord.step_id == context.step_id,
                StepAttemptRecord.run_id == context.run_id,
            )
            .with_for_update()
        )
        if run is None or step is None or attempt is None:
            raise self._reject(
                "model_invocation_context_stale",
                "Model invocation context no longer exists",
            )
        if run.status not in {"QUEUED", "RUNNING", "WAITING"}:
            raise self._reject(
                "model_invocation_run_terminal",
                "Model invocation run is already terminal",
            )
        if step.status != StepStatus.RUNNING.value:
            raise self._reject(
                "model_invocation_step_not_running",
                "Model invocation step is not running",
            )
        if (
            attempt.attempt_no != context.attempt_no
            or attempt.completed_at is not None
            or attempt.deadline_at <= now
        ):
            raise self._reject(
                "model_invocation_attempt_stale",
                "Model invocation attempt is no longer current",
            )
        latest_attempt_no = await uow.session.scalar(
            select(StepAttemptRecord.attempt_no)
            .where(
                StepAttemptRecord.tenant_id == context.tenant_id,
                StepAttemptRecord.step_id == context.step_id,
            )
            .order_by(StepAttemptRecord.attempt_no.desc())
            .limit(1)
        )
        if latest_attempt_no != context.attempt_no:
            raise self._reject(
                "model_invocation_attempt_superseded",
                "Model invocation attempt was superseded",
            )
        return run

    async def reuse(
        self,
        context: ModelInvocationContext,
        request: ModelRequest,
        *,
        provider: str,
        call_no: int,
        logical_call_key: str,
        deadline: datetime,
    ) -> ReusedModelResponse | None:
        async with self._uow_factory(context.tenant_id) as uow:
            source = await uow.session.scalar(
                select(ModelInvocationRecord)
                .where(
                    ModelInvocationRecord.tenant_id == context.tenant_id,
                    ModelInvocationRecord.run_id == context.run_id,
                    ModelInvocationRecord.logical_call_key == logical_call_key,
                    ModelInvocationRecord.status
                    == ModelInvocationStatus.COMPLETED.value,
                    ModelInvocationRecord.output_artifact_uri.is_not(None),
                    ModelInvocationRecord.output_artifact_sha256.is_not(None),
                )
                .order_by(ModelInvocationRecord.completed_at.desc())
                .limit(1)
            )
        if source is None:
            return None
        reference = ArtifactRef(
            str(source.output_artifact_uri),
            str(source.output_artifact_sha256),
        )
        payload = await self._artifacts.get_json(context.tenant_id, reference)
        try:
            response = ProviderResponse(
                text=str(payload["text"]),
                model=str(payload["model"]),
                prompt_tokens=int(payload.get("prompt_tokens", 0)),
                completion_tokens=int(payload.get("completion_tokens", 0)),
                response_id=(
                    str(payload["response_id"])
                    if payload.get("response_id") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._reject(
                "model_reuse_artifact_invalid",
                "Reusable model artifact failed validation",
            ) from exc

        async with self._uow_factory(context.tenant_id) as uow:
            await self._lock_current(uow, context, deadline)
            existing = await uow.session.scalar(
                select(ModelInvocationRecord).where(
                    ModelInvocationRecord.tenant_id == context.tenant_id,
                    ModelInvocationRecord.attempt_id == context.attempt_id,
                    ModelInvocationRecord.role == request.role.value,
                    ModelInvocationRecord.call_no == call_no,
                )
            )
            if existing is not None:
                if (
                    existing.status == ModelInvocationStatus.REUSED.value
                    and existing.reused_from_id == source.id
                ):
                    return ReusedModelResponse(response, source.id)
                raise self._reject(
                    "model_invocation_call_conflict",
                    "Model invocation call number is already reserved",
                )
            now = self._clock.now()
            uow.session.add(
                ModelInvocationRecord(
                    id=self._ids.new_uuid(),
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    attempt_id=context.attempt_id,
                    reused_from_id=source.id,
                    role=request.role.value,
                    provider=provider,
                    model=request.model,
                    call_no=call_no,
                    logical_call_key=logical_call_key,
                    status=ModelInvocationStatus.REUSED.value,
                    billing_disposition=BillingDisposition.NOT_BILLED.value,
                    provider_called=False,
                    trace_id=context.trace_context.trace_id,
                    span_id=context.trace_context.span_id,
                    prompt_template_version=request.prompt_template_version,
                    parser_schema_version=request.parser_schema_version,
                    output_format=request.output_format.value,
                    thinking_mode=request.thinking_mode.value,
                    input_chars=sum(len(message.content) for message in request.messages),
                    output_chars=len(response.text),
                    prompt_tokens=0,
                    completion_tokens=0,
                    output_artifact_uri=reference.uri,
                    output_artifact_sha256=reference.sha256,
                    provider_response_id=response.response_id,
                    started_at=now,
                    completed_at=now,
                )
            )
            await uow.commit()
        return ReusedModelResponse(response, source.id)

    async def start(
        self,
        context: ModelInvocationContext,
        request: ModelRequest,
        *,
        provider: str,
        call_no: int,
        logical_call_key: str,
        deadline: datetime,
    ) -> ModelInvocationReservation:
        async with self._uow_factory(context.tenant_id) as uow:
            run = await self._lock_current(uow, context, deadline)
            existing = await uow.session.scalar(
                select(ModelInvocationRecord).where(
                    ModelInvocationRecord.tenant_id == context.tenant_id,
                    ModelInvocationRecord.attempt_id == context.attempt_id,
                    ModelInvocationRecord.role == request.role.value,
                    ModelInvocationRecord.call_no == call_no,
                )
            )
            if existing is not None:
                raise self._reject(
                    "model_invocation_call_conflict",
                    "Model invocation call number is already reserved",
                )
            budget = dict(run.budget_snapshot)
            usage = dict(run.usage_snapshot or {})
            maximum = int(budget["max_llm_calls"])
            used = int(usage.get("llm_call_count", 0))
            if used + 1 > maximum:
                raise ModelBudgetExceeded("calls", used + 1, maximum)
            usage["llm_call_count"] = used + 1
            run.usage_snapshot = usage
            run.version += 1
            invocation_id = self._ids.new_uuid()
            uow.session.add(
                ModelInvocationRecord(
                    id=invocation_id,
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    attempt_id=context.attempt_id,
                    role=request.role.value,
                    provider=provider,
                    model=request.model,
                    call_no=call_no,
                    logical_call_key=logical_call_key,
                    status=ModelInvocationStatus.STARTED.value,
                    billing_disposition=BillingDisposition.POSSIBLY_BILLED.value,
                    provider_called=True,
                    trace_id=context.trace_context.trace_id,
                    span_id=context.trace_context.span_id,
                    prompt_template_version=request.prompt_template_version,
                    parser_schema_version=request.parser_schema_version,
                    output_format=request.output_format.value,
                    thinking_mode=request.thinking_mode.value,
                    input_chars=sum(len(message.content) for message in request.messages),
                    output_chars=0,
                    prompt_tokens=0,
                    completion_tokens=0,
                    started_at=self._clock.now(),
                )
            )
            await uow.commit()
        return ModelInvocationReservation(invocation_id, call_no, logical_call_key)

    async def complete(
        self,
        reservation: ModelInvocationReservation,
        context: ModelInvocationContext,
        response: ProviderResponse,
    ) -> None:
        output_ref = await self._artifacts.put_json(
            context.tenant_id,
            context.run_id,
            {
                "schema": "sana.model-response.v1",
                "text": response.text,
                "model": response.model,
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "response_id": response.response_id,
            },
        )
        async with self._uow_factory(context.tenant_id) as uow:
            row = await uow.session.scalar(
                select(ModelInvocationRecord)
                .where(
                    ModelInvocationRecord.tenant_id == context.tenant_id,
                    ModelInvocationRecord.id == reservation.id,
                    ModelInvocationRecord.attempt_id == context.attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise self._reject(
                    "model_invocation_missing",
                    "Model invocation audit reservation is missing",
                )
            if row.status == ModelInvocationStatus.COMPLETED.value:
                return
            if row.status != ModelInvocationStatus.STARTED.value:
                raise self._reject(
                    "model_invocation_not_open",
                    "Model invocation audit reservation is already sealed",
                )
            run = await uow.session.scalar(
                select(SearchRunRecord)
                .where(
                    SearchRunRecord.tenant_id == context.tenant_id,
                    SearchRunRecord.id == context.run_id,
                )
                .with_for_update()
            )
            if run is None:
                raise self._reject(
                    "model_invocation_run_missing",
                    "Model invocation run is missing",
                )
            usage = dict(run.usage_snapshot or {})
            usage["prompt_token_count"] = int(
                usage.get("prompt_token_count", 0)
            ) + response.prompt_tokens
            usage["completion_token_count"] = int(
                usage.get("completion_token_count", 0)
            ) + response.completion_tokens
            run.usage_snapshot = usage
            run.version += 1
            row.status = ModelInvocationStatus.COMPLETED.value
            row.billing_disposition = BillingDisposition.BILLED.value
            row.output_chars = len(response.text)
            row.prompt_tokens = response.prompt_tokens
            row.completion_tokens = response.completion_tokens
            row.output_artifact_uri = output_ref.uri
            row.output_artifact_sha256 = output_ref.sha256
            row.provider_response_id = response.response_id
            row.completed_at = self._clock.now()
            await uow.commit()

    async def fail(
        self,
        reservation: ModelInvocationReservation,
        context: ModelInvocationContext,
        error: RedactedInvocationError,
    ) -> None:
        async with self._uow_factory(context.tenant_id) as uow:
            row = await uow.session.scalar(
                select(ModelInvocationRecord)
                .where(
                    ModelInvocationRecord.tenant_id == context.tenant_id,
                    ModelInvocationRecord.id == reservation.id,
                    ModelInvocationRecord.attempt_id == context.attempt_id,
                )
                .with_for_update()
            )
            if row is None:
                raise self._reject(
                    "model_invocation_missing",
                    "Model invocation audit reservation is missing",
                )
            if row.status != ModelInvocationStatus.STARTED.value:
                return
            row.status = ModelInvocationStatus.FAILED.value
            row.billing_disposition = BillingDisposition.POSSIBLY_BILLED.value
            row.error_category = error.category
            row.error_code = error.code
            row.completed_at = self._clock.now()
            await uow.commit()

    async def abandon_attempt(
        self,
        tenant_id: UUID,
        attempt_id: UUID,
        at: datetime,
    ) -> int:
        async with self._uow_factory(tenant_id) as uow:
            rows = (
                await uow.session.scalars(
                    select(ModelInvocationRecord)
                    .where(
                        ModelInvocationRecord.tenant_id == tenant_id,
                        ModelInvocationRecord.attempt_id == attempt_id,
                        ModelInvocationRecord.status
                        == ModelInvocationStatus.STARTED.value,
                    )
                    .with_for_update()
                )
            ).all()
            for row in rows:
                row.status = ModelInvocationStatus.ABANDONED.value
                row.billing_disposition = BillingDisposition.POSSIBLY_BILLED.value
                row.error_category = ErrorCategory.TRANSIENT.value
                row.error_code = "worker_lease_expired"
                row.completed_at = at
            await uow.commit()
            return len(rows)


__all__ = ["SqlModelInvocationAuditSink"]
