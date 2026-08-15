"""Transactional successor scheduling and finalization for durable search Steps."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from sana.app.sql_step_execution import StepCompletionHook
from sana.modules.orchestration.artifact_store import ArtifactStore
from sana.modules.orchestration.domain import (
    AnswerQuality,
    ArtifactRef,
    SearchMode,
    SearchRun,
    SearchStep,
    StepStatus,
    StepType,
    StopReason,
)
from sana.modules.orchestration.executor import ExecutionDisposition
from sana.modules.orchestration.outbox import OutboxMessage
from sana.modules.orchestration.repository import artifact_from_dict
from sana.modules.orchestration.step_handlers import StepExecutionResult
from sana.modules.shared.clock import Clock
from sana.modules.shared.errors import TypedError
from sana.modules.shared.ids import IdFactory, TraceContext
from sana.platform.db.models.conversation import Conversation, Message, ResponseRun
from sana.platform.db.models.orchestration import SearchStepRecord
from sana.platform.db.models.search import (
    AnswerClaim,
    Citation,
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionFetch,
    EvidenceCandidate,
    FactRequirement,
    FetchArtifact,
    ProviderAttempt,
    QuerySpec,
    SearchHit,
    VerifiedEvidence,
)
from sana.platform.db.uow import TenantUnitOfWork


_TERMINAL = {
    StepStatus.SUCCEEDED.value,
    StepStatus.FAILED.value,
    StepStatus.SKIPPED.value,
    StepStatus.CANCELLED.value,
}


def _ref_dict(reference: ArtifactRef) -> dict[str, str]:
    return {"uri": reference.uri, "sha256": reference.sha256}


class WorkflowCompletionCoordinator(StepCompletionHook):
    """Advance one Run while its row lock serializes concurrent completions."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        clock: Clock,
        id_factory: IdFactory,
    ) -> None:
        self._artifacts = artifacts
        self._clock = clock
        self._ids = id_factory

    @staticmethod
    def _fetch_artifact_id(run_id: UUID, fetch_step_key: str) -> UUID:
        if not fetch_step_key.startswith("fetch:"):
            raise ValueError("Fetch artifact identity requires a FETCH step key")
        return uuid5(run_id, f"fetch:{fetch_step_key}")

    async def _payload(
        self,
        tenant_id: UUID,
        reference: ArtifactRef,
    ) -> dict[str, Any]:
        payload = await self._artifacts.get_json(tenant_id, reference)
        if not isinstance(payload, dict):
            raise TypeError("Workflow artifact must contain a JSON object")
        return payload

    @staticmethod
    def _event_type(run: SearchRun, step_type: StepType) -> str:
        if step_type is StepType.FETCH:
            return "STEP_READY_CRAWL"
        return (
            "STEP_READY_RESEARCH"
            if run.mode.value == "RESEARCH"
            else "STEP_READY_FAST"
        )

    async def _add_step(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        *,
        key: str,
        step_type: StepType,
        input_ref: ArtifactRef,
        trace_context: TraceContext,
        plan_revision: int = 1,
    ) -> bool:
        existing = await uow.session.scalar(
            select(SearchStepRecord.id).where(
                SearchStepRecord.tenant_id == run.tenant_id,
                SearchStepRecord.run_id == run.id,
                SearchStepRecord.plan_revision == plan_revision,
                SearchStepRecord.step_key == key,
            )
        )
        if existing is not None:
            return False
        step_id = self._ids.new_uuid()
        await uow.steps.add(
            SearchStep(
                step_id,
                run.tenant_id,
                run.id,
                key,
                step_type,
                plan_revision,
                input_ref,
            )
        )
        now = self._clock.now()
        await uow.outbox.add(
            OutboxMessage(
                id=self._ids.new_uuid(),
                tenant_id=run.tenant_id,
                aggregate_type="search_step",
                aggregate_id=step_id,
                event_type=self._event_type(run, step_type),
                payload={"step_id": str(step_id)},
                trace_context=trace_context.child(self._ids),
                dedupe_key=f"step-ready:{step_id}",
                available_at=now,
                created_at=now,
            )
        )
        return True

    async def _step_rows(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
    ) -> list[SearchStepRecord]:
        return list(
            (
                await uow.session.scalars(
                    select(SearchStepRecord)
                    .where(
                        SearchStepRecord.tenant_id == run.tenant_id,
                        SearchStepRecord.run_id == run.id,
                    )
                    .order_by(SearchStepRecord.created_at, SearchStepRecord.id)
                )
            ).all()
        )

    @staticmethod
    def _status(row: SearchStepRecord, current: SearchStep) -> str:
        return current.status.value if row.id == current.id else row.status

    async def _plan_reference(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        current: SearchStep | None = None,
    ) -> ArtifactRef:
        if (
            current is not None
            and current.step_type is StepType.PLAN
            and current.status is StepStatus.SUCCEEDED
            and current.output_ref is not None
        ):
            return current.output_ref
        value = await uow.session.scalar(
            select(SearchStepRecord.output_ref).where(
                SearchStepRecord.tenant_id == run.tenant_id,
                SearchStepRecord.run_id == run.id,
                SearchStepRecord.step_key == "plan",
                SearchStepRecord.status == StepStatus.SUCCEEDED.value,
            )
        )
        if value is None:
            raise RuntimeError("Run cannot advance without a successful plan artifact")
        return artifact_from_dict(dict(value))

    async def _maybe_select(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        current: SearchStep,
        trace_context: TraceContext,
    ) -> None:
        rows = [
            row
            for row in await self._step_rows(uow, run)
            if row.step_type == StepType.DISCOVERY.value
        ]
        if not rows or any(self._status(row, current) not in _TERMINAL for row in rows):
            return
        references = []
        for row in rows:
            if self._status(row, current) != StepStatus.SUCCEEDED.value:
                continue
            if row.id == current.id:
                if current.output_ref is not None:
                    references.append(_ref_dict(current.output_ref))
            elif row.output_ref is not None:
                references.append(dict(row.output_ref))
        plan_ref = await self._plan_reference(uow, run, current)
        input_ref = await self._artifacts.put_json(
            run.tenant_id,
            run.id,
            {
                "schema": "sana.selection-input.v1",
                "plan_ref": _ref_dict(plan_ref),
                "discovery_refs": references,
            },
        )
        await self._add_step(
            uow,
            run,
            key="select",
            step_type=StepType.SELECT,
            input_ref=input_ref,
            trace_context=trace_context,
        )

    async def _maybe_synthesize(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        current: SearchStep,
        trace_context: TraceContext,
        *,
        degradation_codes: tuple[str, ...] = (),
    ) -> None:
        rows = await self._step_rows(uow, run)
        non_synthesis = [row for row in rows if row.step_type != StepType.SYNTHESIZE.value]
        if any(self._status(row, current) not in _TERMINAL for row in non_synthesis):
            return
        pipeline_degradation_codes = set(degradation_codes)
        for row in non_synthesis:
            status = self._status(row, current)
            if status in {StepStatus.FAILED.value, StepStatus.CANCELLED.value}:
                pipeline_degradation_codes.add(
                    f"{str(row.step_type).lower()}_{status.lower()}"
                )
        plan_ref = await self._plan_reference(uow, run, current)
        verify_refs = [
            dict(row.output_ref)
            for row in rows
            if row.step_type == StepType.VERIFY.value
            and self._status(row, current) == StepStatus.SUCCEEDED.value
            and row.output_ref is not None
        ]
        if (
            current.step_type is StepType.VERIFY
            and current.status is StepStatus.SUCCEEDED
            and current.output_ref is not None
            and _ref_dict(current.output_ref) not in verify_refs
        ):
            verify_refs.append(_ref_dict(current.output_ref))
        if len(verify_refs) > 1:
            raise ValueError("Workflow produced more than one VERIFY output")
        provider_failures = 0
        select_row = next(
            (row for row in rows if row.step_type == StepType.SELECT.value),
            None,
        )
        select_ref = (
            current.output_ref
            if current.step_type is StepType.SELECT
            and current.status is StepStatus.SUCCEEDED
            else artifact_from_dict(dict(select_row.output_ref))
            if select_row is not None and select_row.output_ref is not None
            else None
        )
        if select_ref is not None:
            selection = await self._payload(run.tenant_id, select_ref)
            provider_failures = int(selection.get("provider_failures", 0))
        input_ref = await self._artifacts.put_json(
            run.tenant_id,
            run.id,
            {
                "schema": "sana.synthesis-input.v2",
                "plan_ref": _ref_dict(plan_ref),
                "verify_ref": verify_refs[0] if verify_refs else None,
                "provider_failures": provider_failures,
                "pipeline_degradation_codes": sorted(pipeline_degradation_codes),
            },
        )
        await self._add_step(
            uow,
            run,
            key="synthesize",
            step_type=StepType.SYNTHESIZE,
            input_ref=input_ref,
            trace_context=trace_context,
        )

    async def _maybe_verify(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        current: SearchStep,
        trace_context: TraceContext,
        *,
        degradation_codes: tuple[str, ...] = (),
    ) -> None:
        rows = await self._step_rows(uow, run)
        fetch_extract = [
            row
            for row in rows
            if row.step_type in {StepType.FETCH.value, StepType.EXTRACT.value}
        ]
        all_fetch_extract_terminal = all(
            self._status(row, current) in _TERMINAL for row in fetch_extract
        )
        extract_refs = [
            dict(row.output_ref)
            for row in rows
            if row.step_type == StepType.EXTRACT.value
            and self._status(row, current) == StepStatus.SUCCEEDED.value
            and row.output_ref is not None
        ]
        if (
            current.step_type is StepType.EXTRACT
            and current.status is StepStatus.SUCCEEDED
            and current.output_ref is not None
            and _ref_dict(current.output_ref) not in extract_refs
        ):
            extract_refs.append(_ref_dict(current.output_ref))
        existing_verify = next(
            (row for row in rows if row.step_type == StepType.VERIFY.value),
            None,
        )
        if existing_verify is not None:
            if (
                all_fetch_extract_terminal
                and self._status(existing_verify, current) in _TERMINAL
            ):
                await self._maybe_synthesize(
                    uow,
                    run,
                    current,
                    trace_context,
                    degradation_codes=degradation_codes,
                )
            return
        if not extract_refs:
            if all_fetch_extract_terminal:
                await self._maybe_synthesize(
                    uow,
                    run,
                    current,
                    trace_context,
                    degradation_codes=degradation_codes,
                )
            return
        plan_ref = await self._plan_reference(uow, run, current)
        if not all_fetch_extract_terminal:
            if run.mode is not SearchMode.FAST:
                return
            extracted_payloads = [
                await self._payload(
                    run.tenant_id,
                    artifact_from_dict(dict(reference)),
                )
                for reference in extract_refs
            ]
            if any(
                str(dict(payload.get("hit", {})).get("provider", "")) != "direct"
                for payload in extracted_payloads
            ):
                return
            plan = await self._payload(run.tenant_id, plan_ref)
            required_fact_ids = {
                str(fact["id"])
                for fact in plan.get("facts", ())
                if bool(fact.get("required", True))
            }
            extracted_fact_ids = {
                str(fact_id)
                for payload in extracted_payloads
                for fact_id in (
                    dict(payload.get("hit", {})).get("fact_ids")
                    or (dict(payload.get("hit", {})).get("fact_id"),)
                )
                if fact_id is not None
            }
            if not required_fact_ids or not required_fact_ids <= extracted_fact_ids:
                return
        input_ref = await self._artifacts.put_json(
            run.tenant_id,
            run.id,
            {
                "schema": "sana.verify-input.v2",
                "plan_ref": _ref_dict(plan_ref),
                "extract_refs": extract_refs,
            },
        )
        await self._add_step(
            uow,
            run,
            key="verify",
            step_type=StepType.VERIFY,
            input_ref=input_ref,
            trace_context=trace_context,
        )

    async def _persist_plan(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        payload: dict[str, Any],
    ) -> None:
        facts = list(payload.get("facts", []))
        queries = list(payload.get("queries", []))
        for fact in facts:
            await uow.session.execute(
                insert(FactRequirement)
                .values(
                    id=UUID(str(fact["id"])),
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    fact_key=str(fact["key"]),
                    description=str(fact["description"]),
                    required=bool(fact.get("required", True)),
                    freshness=str(fact["freshness"]),
                    consequence=str(fact["consequence"]),
                    status="OPEN",
                )
                .on_conflict_do_nothing()
            )
        for query in queries:
            await uow.session.execute(
                insert(QuerySpec)
                .values(
                    id=UUID(str(query["id"])),
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    fact_requirement_id=UUID(str(query["fact_id"])),
                    plan_revision=int(query["plan_revision"]),
                    query_key=str(query["key"]),
                    query_text=str(query["text"]),
                    provider_class=",".join(map(str, payload.get("providers", []))),
                    locale=str(query["locale"]),
                    freshness_days=query.get("freshness_days"),
                    query_metadata=dict(query.get("metadata", {})),
                )
                .on_conflict_do_nothing()
            )

    async def _persist_discovery(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        payload: dict[str, Any],
    ) -> None:
        now = self._clock.now()
        query = dict(payload["query"])
        for response in payload.get("responses", []):
            provider = str(response["provider"])
            attempt_id = uuid5(run.id, f"provider:{step.step_key}:{provider}")
            error = response.get("error")
            metrics = dict(response["metrics"])
            await uow.session.execute(
                insert(ProviderAttempt)
                .values(
                    id=attempt_id,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    query_spec_id=UUID(str(query["id"])),
                    attempt_no=1,
                    provider=provider,
                    status="FAILED" if error else "SUCCEEDED",
                    started_at=now,
                    completed_at=now,
                    latency_ms=int(metrics["elapsed_ms"]),
                    error_type=(str(error["category"]) if error else None),
                    error_code=(str(error["code"]) if error else None),
                )
                .on_conflict_do_nothing()
            )
            for hit in response.get("hits", []):
                canonical = str(hit["canonical_url"])
                await uow.session.execute(
                    insert(SearchHit)
                    .values(
                        id=UUID(str(hit["id"])),
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        provider_attempt_id=attempt_id,
                        rank=int(hit["rank"]),
                        url=str(hit["url"]),
                        canonical_url=canonical,
                        url_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                        title=str(hit.get("title", "")),
                        snippet=str(hit.get("snippet", "")),
                        score=float(hit["score"]),
                        discovered_at=now,
                    )
                    .on_conflict_do_nothing()
                )

    async def _persist_fetch(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        payload: dict[str, Any],
    ) -> None:
        hit = dict(payload["hit"])
        canonical = str(hit["canonical_url"])
        record_id = self._fetch_artifact_id(run.id, step.step_key)
        await uow.session.execute(
            insert(FetchArtifact)
            .values(
                id=record_id,
                tenant_id=run.tenant_id,
                run_id=run.id,
                search_hit_id=UUID(str(hit["id"])),
                url=canonical,
                url_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                attempt_no=1,
                fetcher="http",
                status="SUCCEEDED",
                http_status=int(payload["http_status"]),
                media_type=str(payload["media_type"]),
                content_hash=str(payload["content_hash"]),
                storage_uri=str(payload["body_ref"]["uri"]),
                response_bytes=None,
                fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
                fetch_metadata={"redirects": list(payload.get("redirects", []))},
            )
            .on_conflict_do_nothing()
        )

    async def _persist_extract(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        payload: dict[str, Any],
    ) -> None:
        document = dict(payload["document"])
        version = dict(payload["version"])
        fetch_step_key = step.step_key.replace("extract:", "fetch:", 1)
        if fetch_step_key == step.step_key:
            raise ValueError("Extract lineage requires an EXTRACT step key")
        fetch_artifact_id = self._fetch_artifact_id(run.id, fetch_step_key)
        await uow.session.execute(
            insert(Document)
            .values(
                id=UUID(str(document["id"])),
                tenant_id=run.tenant_id,
                canonical_url=str(document["canonical_url"]),
                canonical_url_hash=str(document["canonical_url_hash"]),
                title=str(document["title"]),
                source_host=str(document["source_host"]),
            )
            .on_conflict_do_nothing()
        )
        await uow.session.execute(
            insert(DocumentVersion)
            .values(
                id=UUID(str(version["id"])),
                tenant_id=run.tenant_id,
                document_id=UUID(str(document["id"])),
                fetch_artifact_id=fetch_artifact_id,
                content_hash=str(version["content_hash"]),
                storage_uri=str(version["text_ref"]["uri"]),
                media_type=str(version["media_type"]),
                language=version.get("language"),
                text_length=int(version["text_length"]),
                fetched_at=datetime.fromisoformat(str(version["fetched_at"])),
                document_metadata={},
            )
            .on_conflict_do_nothing()
        )
        version_id = UUID(str(version["id"]))
        await uow.session.execute(
            insert(DocumentVersionFetch)
            .values(
                id=uuid5(
                    run.id,
                    f"document-version-fetch:{version_id}:{fetch_artifact_id}",
                ),
                tenant_id=run.tenant_id,
                run_id=run.id,
                document_version_id=version_id,
                fetch_artifact_id=fetch_artifact_id,
                created_at=self._clock.now(),
            )
            .on_conflict_do_nothing()
        )
        for chunk in payload.get("chunks", []):
            await uow.session.execute(
                insert(DocumentChunk)
                .values(
                    id=UUID(str(chunk["id"])),
                    tenant_id=run.tenant_id,
                    document_version_id=UUID(str(version["id"])),
                    ordinal=int(chunk["ordinal"]),
                    text_content=str(chunk["text"]),
                    text_hash=str(chunk["text_hash"]),
                    token_count=int(chunk["token_count"]),
                    start_offset=int(chunk["start_offset"]),
                    end_offset=int(chunk["end_offset"]),
                )
                .on_conflict_do_nothing()
            )

    async def _persist_verification(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        payload: dict[str, Any],
    ) -> None:
        for evidence in payload.get("evidence", ()):
            await uow.session.execute(
                insert(EvidenceCandidate)
                .values(
                    id=UUID(str(evidence["candidate_id"])),
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    fact_requirement_id=UUID(str(evidence["fact_id"])),
                    document_version_id=UUID(str(evidence["document_version_id"])),
                    document_chunk_id=UUID(str(evidence["document_chunk_id"])),
                    quote=str(evidence["quote"]),
                    quote_hash=str(evidence["quote_hash"]),
                    start_offset=int(evidence["start_offset"]),
                    end_offset=int(evidence["end_offset"]),
                    support_type=str(evidence["support_type"]),
                    candidate_score=float(evidence["candidate_score"]),
                    source_identity=str(evidence["source_identity"]),
                    source_authority=str(evidence["source_authority"]),
                )
                .on_conflict_do_nothing()
            )
            await uow.session.execute(
                insert(VerifiedEvidence)
                .values(
                    id=UUID(str(evidence["verified_id"])),
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    candidate_id=UUID(str(evidence["candidate_id"])),
                    verdict=str(evidence["verdict"]),
                    confidence=float(evidence["confidence"]),
                    reason_codes=list(evidence["reason_codes"]),
                    verifier_version=str(evidence["verifier_version"]),
                    verified_at=datetime.fromisoformat(str(evidence["verified_at"])),
                )
                .on_conflict_do_nothing()
            )
        for assessment in payload.get("coverage", ()):
            await uow.session.execute(
                update(FactRequirement)
                .where(
                    FactRequirement.tenant_id == run.tenant_id,
                    FactRequirement.id == UUID(str(assessment["fact_id"])),
                )
                .values(status=str(assessment["status"]))
            )

    async def _persist_answer(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        payload: dict[str, Any],
    ) -> None:
        for claim in payload.get("claims", []):
            claim_id = UUID(str(claim["id"]))
            claim_kind = claim.get("kind")
            await uow.session.execute(
                insert(AnswerClaim)
                .values(
                    id=claim_id,
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    claim_key=str(claim["claim_key"]),
                    claim_text=str(claim["text"]),
                    claim_kind=str(claim_kind) if claim_kind is not None else None,
                    fact_requirement_id=(
                        UUID(str(claim["fact_id"]))
                        if claim.get("fact_id") is not None
                        else None
                    ),
                    support_status=str(claim["support_status"]),
                )
                .on_conflict_do_nothing()
            )
            for citation in claim.get("citations", ()):
                await uow.session.execute(
                    insert(Citation)
                    .values(
                        id=UUID(str(citation["id"])),
                        tenant_id=run.tenant_id,
                        run_id=run.id,
                        answer_claim_id=claim_id,
                        verified_evidence_id=UUID(str(citation["evidence_id"])),
                        ordinal=int(citation["ordinal"]),
                        label=str(citation["label"]),
                        rendered_url=str(citation["url"]),
                        document_version_id=UUID(
                            str(citation["document_version_id"])
                        ),
                        document_chunk_id=UUID(str(citation["document_chunk_id"])),
                        quote=str(citation["quote"]),
                        start_offset=int(citation["start_offset"]),
                        end_offset=int(citation["end_offset"]),
                    )
                    .on_conflict_do_nothing()
                )
        message_id = uuid5(run.id, "assistant-message")
        now = self._clock.now()
        await uow.session.execute(
            insert(Message)
            .values(
                id=message_id,
                tenant_id=run.tenant_id,
                conversation_id=run.conversation_id,
                author_user_id=None,
                role="ASSISTANT",
                content=str(payload["answer"]),
                message_metadata={"search_run_id": str(run.id)},
                idempotency_key=f"assistant:{run.id}",
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        await uow.session.execute(
            update(ResponseRun)
            .where(
                ResponseRun.tenant_id == run.tenant_id,
                ResponseRun.id == run.response_run_id,
            )
            .values(status="SUCCEEDED", output_message_id=message_id, updated_at=now)
        )
        await uow.session.execute(
            update(Conversation)
            .where(
                Conversation.tenant_id == run.tenant_id,
                Conversation.id == run.conversation_id,
            )
            .values(updated_at=now)
        )

    async def _fail_run(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        reason: StopReason,
    ) -> None:
        if not run.is_terminal:
            run.fail(reason, self._clock.now())
        await uow.session.execute(
            update(ResponseRun)
            .where(
                ResponseRun.tenant_id == run.tenant_id,
                ResponseRun.id == run.response_run_id,
            )
            .values(status="FAILED", updated_at=self._clock.now())
        )

    async def on_success(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        result: StepExecutionResult,
        trace_context: TraceContext,
    ) -> None:
        payload = await self._payload(run.tenant_id, result.output_ref)
        if step.step_type is StepType.ROUTE:
            await self._add_step(
                uow,
                run,
                key="plan",
                step_type=StepType.PLAN,
                input_ref=result.output_ref,
                trace_context=trace_context,
            )
            return
        if step.step_type is StepType.PLAN:
            await self._persist_plan(uow, run, payload)
            queries = list(payload.get("queries", []))
            for query in queries:
                input_ref = await self._artifacts.put_json(
                    run.tenant_id,
                    run.id,
                    {
                        "schema": "sana.discovery-input.v1",
                        "plan_ref": _ref_dict(result.output_ref),
                        "query": query,
                        "providers": list(payload.get("providers", [])),
                    },
                )
                await self._add_step(
                    uow,
                    run,
                    key=f"discover:{query['key']}",
                    step_type=StepType.DISCOVERY,
                    input_ref=input_ref,
                    trace_context=trace_context,
                )
            if not queries:
                await self._maybe_synthesize(uow, run, step, trace_context)
            return
        if step.step_type is StepType.DISCOVERY:
            await self._persist_discovery(uow, run, step, payload)
            await self._maybe_select(uow, run, step, trace_context)
            return
        if step.step_type is StepType.SELECT:
            selected = list(payload.get("selected", []))
            for index, hit in enumerate(selected, start=1):
                input_ref = await self._artifacts.put_json(
                    run.tenant_id,
                    run.id,
                    {
                        "schema": "sana.fetch-input.v1",
                        "plan_ref": payload["plan_ref"],
                        "hit": hit,
                    },
                )
                await self._add_step(
                    uow,
                    run,
                    key=f"fetch:{index}:{hit['id']}",
                    step_type=StepType.FETCH,
                    input_ref=input_ref,
                    trace_context=trace_context,
                )
            if not selected:
                await self._maybe_synthesize(uow, run, step, trace_context)
            return
        if step.step_type is StepType.FETCH:
            await self._persist_fetch(uow, run, step, payload)
            await self._add_step(
                uow,
                run,
                key=step.step_key.replace("fetch:", "extract:", 1),
                step_type=StepType.EXTRACT,
                input_ref=result.output_ref,
                trace_context=trace_context,
            )
            return
        if step.step_type is StepType.EXTRACT:
            await self._persist_extract(uow, run, step, payload)
            await self._maybe_verify(uow, run, step, trace_context)
            return
        if step.step_type is StepType.VERIFY:
            await self._persist_verification(uow, run, payload)
            await self._maybe_synthesize(uow, run, step, trace_context)
            return
        if step.step_type is StepType.SYNTHESIZE:
            await self._persist_answer(uow, run, payload)
            run.succeed(
                AnswerQuality(str(payload["quality"])),
                StopReason(str(payload["stop_reason"])),
                self._clock.now(),
            )

    async def on_failure(
        self,
        uow: TenantUnitOfWork,
        run: SearchRun,
        step: SearchStep,
        error: TypedError,
        disposition: ExecutionDisposition,
        trace_context: TraceContext,
    ) -> None:
        if disposition is ExecutionDisposition.RETRY_SCHEDULED or run.is_terminal:
            return
        if step.step_type in {StepType.ROUTE, StepType.PLAN, StepType.SYNTHESIZE}:
            await self._fail_run(uow, run, StopReason.INFRASTRUCTURE_FAILURE)
            return
        if step.step_type is StepType.DISCOVERY:
            await self._maybe_select(uow, run, step, trace_context)
            return
        if step.step_type in {StepType.FETCH, StepType.EXTRACT}:
            await self._maybe_verify(
                uow,
                run,
                step,
                trace_context,
                degradation_codes=(error.code,),
            )
            return
        await self._maybe_synthesize(
            uow,
            run,
            step,
            trace_context,
            degradation_codes=(error.code,),
        )


__all__ = ["WorkflowCompletionCoordinator"]
