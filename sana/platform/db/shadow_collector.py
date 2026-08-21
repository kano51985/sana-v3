"""PostgreSQL adapters for read-only snapshots and fenced Collector commits."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Mapping
from uuid import UUID, uuid5

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sana.modules.shadow_campaign.collector import (
    CollectionOutcome,
    CollectionReceipt,
    CollectorLease,
    RunSourceSnapshot,
    SourceAttempt,
    SourceCitation,
    SourceClaim,
    SourceEvidence,
    SourceFact,
    SourceFetch,
    SourceInvocation,
    SourceOutbox,
    SourceProviderAttempt,
    SourceQuery,
    SourceStep,
)
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ErrorClass,
    SchedulingState,
)
from sana.modules.shadow_campaign.policy import CostRate
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.conversation import Message, ResponseRun
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.orchestration import (
    OutboxEvent,
    SearchRunRecord,
    SearchStepRecord,
    StepAttemptRecord,
)
from sana.platform.db.models.search import (
    AnswerClaim,
    Citation,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionFetch,
    EvidenceCandidate,
    FactRequirement,
    FetchArtifact,
    ProviderAttempt,
    QuerySpec,
    VerifiedEvidence,
)
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowGoldAssertionResultRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.shadow_campaign_repository import SqlShadowCampaignRepository


_COLLECTABLE_CAMPAIGN_STATUSES = frozenset(
    {
        CampaignStatus.RUNNING.value,
        CampaignStatus.STOPPING.value,
        CampaignStatus.PAUSED.value,
    }
)
_TERMINAL_RUN_STATUSES = ("SUCCEEDED", "FAILED", "CANCELLED")


def _ledger_int(payload: object, key: str, *, default: int | None = None) -> int:
    if not isinstance(payload, dict):
        raise InvariantViolation(
            "SearchRun usage ledger is not an object",
            code="source_usage_ledger_invalid",
        )
    value = payload.get(key, default)
    if isinstance(value, bool) or value is None:
        raise InvariantViolation(
            "SearchRun usage ledger field is missing or invalid",
            code="source_usage_ledger_invalid",
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError, ArithmeticError) as error:
        raise InvariantViolation(
            "SearchRun usage ledger field is invalid",
            code="source_usage_ledger_invalid",
        ) from error
    if parsed < 0:
        raise InvariantViolation(
            "SearchRun usage ledger field is negative",
            code="source_usage_ledger_invalid",
        )
    return parsed


_CACHE_FETCH_METADATA_KEYS = frozenset(
    {
        "redirects",
        "policy_version",
        "strictest_freshness",
        "source_fetch_artifact_id",
        "source_run_id",
        "source_document_version_id",
        "source_fetched_at",
        "reused_at",
        "reuse_age_seconds",
        "decision",
        "live_error_category",
        "live_error_code",
    }
)
_CACHE_FETCH_REQUIRED_KEYS = frozenset(
    {
        "policy_version",
        "strictest_freshness",
        "source_fetch_artifact_id",
        "source_run_id",
        "source_document_version_id",
        "source_fetched_at",
        "reused_at",
        "reuse_age_seconds",
        "decision",
    }
)


def _source_fetch(item: FetchArtifact) -> SourceFetch:
    metadata = item.fetch_metadata
    if not isinstance(metadata, Mapping):
        raise InvariantViolation(
            "FetchArtifact metadata is not an object",
            code="source_fetch_metadata_invalid",
        )
    if item.fetcher != "document-cache":
        return SourceFetch(
            id=item.id,
            fetcher=item.fetcher,
            status=item.status,
            fetched_at=item.fetched_at,
            decision="LIVE",
        )
    if (
        set(metadata) - _CACHE_FETCH_METADATA_KEYS
        or not _CACHE_FETCH_REQUIRED_KEYS.issubset(metadata)
    ):
        raise InvariantViolation(
            "Cached FetchArtifact metadata is not allowlisted or complete",
            code="source_fetch_metadata_invalid",
        )
    policy_version = metadata["policy_version"]
    strictest_freshness = metadata["strictest_freshness"]
    decision = metadata["decision"]
    raw_reuse_age_seconds = metadata["reuse_age_seconds"]
    if (
        not isinstance(policy_version, str)
        or not policy_version
        or strictest_freshness not in {"STABLE", "RECENT", "CURRENT"}
        or decision not in {"CACHE_FRESH", "CACHE_STALE_IF_ERROR"}
        or isinstance(raw_reuse_age_seconds, bool)
    ):
        raise InvariantViolation(
            "Cached FetchArtifact policy metadata is invalid",
            code="source_fetch_metadata_invalid",
        )
    try:
        source_fetched_at = datetime.fromisoformat(
            str(metadata["source_fetched_at"])
        )
        reused_at = datetime.fromisoformat(str(metadata["reused_at"]))
        reuse_age_seconds = int(raw_reuse_age_seconds)
        source_fetch_artifact_id = UUID(
            str(metadata["source_fetch_artifact_id"])
        )
        source_run_id = UUID(str(metadata["source_run_id"]))
        source_document_version_id = UUID(
            str(metadata["source_document_version_id"])
        )
    except (TypeError, ValueError, ArithmeticError) as error:
        raise InvariantViolation(
            "Cached FetchArtifact metadata cannot be parsed",
            code="source_fetch_metadata_invalid",
        ) from error
    live_error_category = metadata.get("live_error_category")
    live_error_code = metadata.get("live_error_code")
    if any(
        value is not None and (not isinstance(value, str) or not value)
        for value in (live_error_category, live_error_code)
    ):
        raise InvariantViolation(
            "Cached FetchArtifact error metadata is invalid",
            code="source_fetch_metadata_invalid",
        )
    return SourceFetch(
        id=item.id,
        fetcher=item.fetcher,
        status=item.status,
        fetched_at=item.fetched_at,
        decision=decision,
        policy_version=policy_version,
        strictest_freshness=strictest_freshness,
        source_fetch_artifact_id=source_fetch_artifact_id,
        source_run_id=source_run_id,
        source_document_version_id=source_document_version_id,
        source_fetched_at=source_fetched_at,
        reused_at=reused_at,
        reuse_age_seconds=reuse_age_seconds,
        live_error_category=live_error_category,
        live_error_code=live_error_code,
    )


class SqlShadowSnapshotReader:
    """Build one authoritative snapshot in a tenant-scoped read-only transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self, lease: CollectorLease) -> RunSourceSnapshot:
        session = self._session_factory()
        try:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(lease.tenant_id)},
            )
            binding = (
                await session.execute(
                    select(
                        ShadowRunResultRecord.conversation_id,
                        ShadowRunResultRecord.search_run_id,
                        ShadowRunResultRecord.scheduling_state,
                        ShadowRunResultRecord.collector_lease_owner,
                    ).where(
                        ShadowRunResultRecord.tenant_id == lease.tenant_id,
                        ShadowRunResultRecord.campaign_id == lease.campaign_id,
                        ShadowRunResultRecord.id == lease.id,
                    )
                )
            ).one_or_none()
            if (
                binding is None
                or binding.conversation_id != lease.conversation_id
                or binding.search_run_id != lease.search_run_id
                or not (
                    (
                        binding.scheduling_state == SchedulingState.SUBMITTED.value
                        and binding.collector_lease_owner == lease.lease_owner
                    )
                    or (
                        binding.scheduling_state == SchedulingState.COLLECTED.value
                        and binding.collector_lease_owner is None
                    )
                )
            ):
                raise InvariantViolation(
                    "Collector source binding is no longer authoritative",
                    code="collector_source_binding_changed",
                )

            run_row = (
                await session.execute(
                    select(SearchRunRecord, ResponseRun, Message)
                    .join(
                        ResponseRun,
                        (ResponseRun.tenant_id == SearchRunRecord.tenant_id)
                        & (ResponseRun.id == SearchRunRecord.response_run_id),
                    )
                    .outerjoin(
                        Message,
                        (Message.tenant_id == ResponseRun.tenant_id)
                        & (Message.id == ResponseRun.output_message_id),
                    )
                    .where(
                        SearchRunRecord.tenant_id == lease.tenant_id,
                        SearchRunRecord.id == lease.search_run_id,
                        SearchRunRecord.conversation_id == lease.conversation_id,
                    )
                )
            ).one_or_none()
            if run_row is None:
                raise InvariantViolation(
                    "Collector SearchRun source is missing",
                    code="collector_source_missing",
                )
            run, response, output_message = run_row

            facts = tuple(
                SourceFact(
                    item.id,
                    item.required,
                    item.status,
                    item.freshness,
                    item.consequence,
                )
                for item in (
                    await session.scalars(
                        select(FactRequirement).where(
                            FactRequirement.tenant_id == lease.tenant_id,
                            FactRequirement.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            queries = tuple(
                SourceQuery(
                    item.id,
                    item.fact_requirement_id,
                    item.plan_revision,
                    item.provider_class,
                    item.query_text,
                )
                for item in (
                    await session.scalars(
                        select(QuerySpec).where(
                            QuerySpec.tenant_id == lease.tenant_id,
                            QuerySpec.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            provider_attempts = tuple(
                SourceProviderAttempt(
                    item.id,
                    item.query_spec_id,
                    item.provider,
                    item.status,
                    item.started_at,
                    item.completed_at,
                    item.error_type,
                )
                for item in (
                    await session.scalars(
                        select(ProviderAttempt).where(
                            ProviderAttempt.tenant_id == lease.tenant_id,
                            ProviderAttempt.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            step_records = tuple(
                (
                    await session.scalars(
                        select(SearchStepRecord).where(
                            SearchStepRecord.tenant_id == lease.tenant_id,
                            SearchStepRecord.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            steps = tuple(
                SourceStep(
                    item.id,
                    item.step_key,
                    item.step_type,
                    item.plan_revision,
                    item.status,
                    item.output_ref is not None,
                )
                for item in step_records
            )
            attempts = tuple(
                SourceAttempt(
                    item.id,
                    item.step_id,
                    item.attempt_no,
                    item.started_at,
                    item.completed_at,
                    item.error_type,
                )
                for item in (
                    await session.scalars(
                        select(StepAttemptRecord).where(
                            StepAttemptRecord.tenant_id == lease.tenant_id,
                            StepAttemptRecord.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            invocations = tuple(
                SourceInvocation(
                    item.id,
                    item.step_id,
                    item.attempt_id,
                    item.role,
                    item.provider,
                    item.model,
                    item.call_no,
                    item.status,
                    item.billing_disposition,
                    item.provider_called,
                    item.prompt_tokens,
                    item.completion_tokens,
                    item.started_at,
                    item.completed_at,
                    item.error_category,
                )
                for item in (
                    await session.scalars(
                        select(ModelInvocationRecord).where(
                            ModelInvocationRecord.tenant_id == lease.tenant_id,
                            ModelInvocationRecord.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            fetches = tuple(
                _source_fetch(item)
                for item in (
                    await session.scalars(
                        select(FetchArtifact).where(
                            FetchArtifact.tenant_id == lease.tenant_id,
                            FetchArtifact.run_id == lease.search_run_id,
                            FetchArtifact.status == "SUCCEEDED",
                        )
                    )
                ).all()
            )
            cached_fetches = tuple(
                item for item in fetches if item.fetcher == "document-cache"
            )
            if cached_fetches:
                source_fetch_ids = tuple(
                    item.source_fetch_artifact_id for item in cached_fetches
                )
                source_lineage_rows = (
                    await session.execute(
                        select(
                            FetchArtifact.id,
                            FetchArtifact.run_id,
                            FetchArtifact.fetched_at,
                            DocumentVersionFetch.document_version_id,
                        )
                        .join(
                            DocumentVersionFetch,
                            (
                                DocumentVersionFetch.tenant_id
                                == FetchArtifact.tenant_id
                            )
                            & (
                                DocumentVersionFetch.run_id
                                == FetchArtifact.run_id
                            )
                            & (
                                DocumentVersionFetch.fetch_artifact_id
                                == FetchArtifact.id
                            ),
                        )
                        .where(
                            FetchArtifact.tenant_id == lease.tenant_id,
                            DocumentVersionFetch.tenant_id == lease.tenant_id,
                            FetchArtifact.id.in_(source_fetch_ids),
                            FetchArtifact.status == "SUCCEEDED",
                            FetchArtifact.fetcher == "http",
                        )
                    )
                ).all()
                valid_source_lineage = {
                    (
                        row.id,
                        row.run_id,
                        row.fetched_at,
                        row.document_version_id,
                    )
                    for row in source_lineage_rows
                }
                if any(
                    (
                        item.source_fetch_artifact_id,
                        item.source_run_id,
                        item.source_fetched_at,
                        item.source_document_version_id,
                    )
                    not in valid_source_lineage
                    for item in cached_fetches
                ):
                    raise InvariantViolation(
                        "Cached FetchArtifact source lineage is invalid",
                        code="source_fetch_lineage_invalid",
                    )

            evidence_rows = (
                await session.execute(
                    select(
                        VerifiedEvidence,
                        EvidenceCandidate,
                        DocumentChunk,
                        DocumentVersion,
                    )
                    .outerjoin(
                        EvidenceCandidate,
                        (EvidenceCandidate.tenant_id == VerifiedEvidence.tenant_id)
                        & (EvidenceCandidate.id == VerifiedEvidence.candidate_id),
                    )
                    .outerjoin(
                        DocumentChunk,
                        (DocumentChunk.tenant_id == EvidenceCandidate.tenant_id)
                        & (DocumentChunk.id == EvidenceCandidate.document_chunk_id),
                    )
                    .outerjoin(
                        DocumentVersion,
                        (DocumentVersion.tenant_id == EvidenceCandidate.tenant_id)
                        & (
                            DocumentVersion.id
                            == EvidenceCandidate.document_version_id
                        ),
                    )
                    .where(
                        VerifiedEvidence.tenant_id == lease.tenant_id,
                        VerifiedEvidence.run_id == lease.search_run_id,
                    )
                )
            ).all()
            valid_lineage_versions = set(
                (
                    await session.scalars(
                        select(DocumentVersionFetch.document_version_id)
                        .join(
                            FetchArtifact,
                            (
                                FetchArtifact.tenant_id
                                == DocumentVersionFetch.tenant_id
                            )
                            & (
                                FetchArtifact.id
                                == DocumentVersionFetch.fetch_artifact_id
                            ),
                        )
                        .where(
                            DocumentVersionFetch.tenant_id == lease.tenant_id,
                            DocumentVersionFetch.run_id == lease.search_run_id,
                            FetchArtifact.run_id == lease.search_run_id,
                            FetchArtifact.status == "SUCCEEDED",
                        )
                    )
                ).all()
            )
            evidence_items: list[SourceEvidence] = []
            for verified, candidate, chunk, version in evidence_rows:
                if candidate is None:
                    raise InvariantViolation(
                        "VerifiedEvidence has no tenant-visible candidate",
                        code="source_evidence_chain_invalid",
                    )
                evidence_items.append(
                    SourceEvidence(
                        verified.id,
                        candidate.id,
                        candidate.fact_requirement_id,
                        candidate.document_version_id,
                        candidate.document_chunk_id,
                        candidate.start_offset,
                        candidate.end_offset,
                        len(candidate.quote),
                        candidate.support_type,
                        candidate.source_authority,
                        verified.verdict,
                        verified.confidence,
                        tuple(verified.reason_codes),
                        verified.verifier_version,
                        verified.verified_at,
                        bool(
                            candidate.run_id == lease.search_run_id
                            and chunk is not None
                            and chunk.document_version_id
                            == candidate.document_version_id
                            and version is not None
                            and version.id == candidate.document_version_id
                            and candidate.document_version_id
                            in valid_lineage_versions
                        ),
                    )
                )

            claims = tuple(
                SourceClaim(
                    item.id,
                    item.claim_kind,
                    item.fact_requirement_id,
                    item.support_status,
                    item.claim_text,
                )
                for item in (
                    await session.scalars(
                        select(AnswerClaim).where(
                            AnswerClaim.tenant_id == lease.tenant_id,
                            AnswerClaim.run_id == lease.search_run_id,
                        )
                    )
                ).all()
            )
            citation_rows = (
                await session.execute(
                    select(
                        Citation,
                        func.char_length(Citation.quote).label("quote_length"),
                        (Citation.quote == EvidenceCandidate.quote).label(
                            "quote_matches_evidence"
                        ),
                    )
                    .outerjoin(
                        VerifiedEvidence,
                        (VerifiedEvidence.tenant_id == Citation.tenant_id)
                        & (VerifiedEvidence.id == Citation.verified_evidence_id),
                    )
                    .outerjoin(
                        EvidenceCandidate,
                        (EvidenceCandidate.tenant_id == VerifiedEvidence.tenant_id)
                        & (EvidenceCandidate.id == VerifiedEvidence.candidate_id),
                    )
                    .where(
                        Citation.tenant_id == lease.tenant_id,
                        Citation.run_id == lease.search_run_id,
                    )
                )
            ).all()
            citations = tuple(
                SourceCitation(
                    item.id,
                    item.answer_claim_id,
                    item.verified_evidence_id,
                    item.document_version_id,
                    item.document_chunk_id,
                    item.start_offset,
                    item.end_offset,
                    int(quote_length),
                    bool(quote_matches),
                )
                for item, quote_length, quote_matches in citation_rows
            )
            aggregate_ids = [lease.search_run_id, response.id, *(item.id for item in step_records)]
            outbox = tuple(
                SourceOutbox(
                    item.id,
                    item.aggregate_type,
                    item.aggregate_id,
                    item.event_type,
                    item.created_at,
                    item.published_at,
                )
                for item in (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.tenant_id == lease.tenant_id,
                            OutboxEvent.aggregate_id.in_(aggregate_ids),
                        )
                    )
                ).all()
            )
            return RunSourceSnapshot(
                tenant_id=run.tenant_id,
                run_id=run.id,
                conversation_id=run.conversation_id,
                response_run_id=run.response_run_id,
                response_status=response.status,
                output_message_id=response.output_message_id,
                output_message_role=(output_message.role if output_message else None),
                output_message_conversation_id=(
                    output_message.conversation_id if output_message else None
                ),
                answer_text=output_message.content if output_message else None,
                mode=run.mode,
                status=run.status,
                answer_quality=run.answer_quality,
                stop_reason=run.stop_reason,
                created_at=run.created_at,
                hard_deadline_at=run.hard_deadline_at,
                completed_at=run.completed_at,
                version=run.version,
                budget_max_llm_calls=_ledger_int(
                    run.budget_snapshot,
                    "max_llm_calls",
                ),
                recorded_llm_call_count=_ledger_int(
                    run.usage_snapshot,
                    "llm_call_count",
                    default=0,
                ),
                recorded_prompt_tokens=_ledger_int(
                    run.usage_snapshot,
                    "prompt_token_count",
                    default=0,
                ),
                recorded_completion_tokens=_ledger_int(
                    run.usage_snapshot,
                    "completion_token_count",
                    default=0,
                ),
                facts=facts,
                queries=queries,
                provider_attempts=provider_attempts,
                steps=steps,
                attempts=attempts,
                invocations=invocations,
                fetches=fetches,
                evidence=tuple(evidence_items),
                claims=claims,
                citations=citations,
                outbox=outbox,
            )
        finally:
            await session.rollback()
            await session.close()


class SqlShadowCollectorRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _assert_tenant(self, tenant_id: UUID) -> None:
        if tenant_id != self._tenant_id:
            raise InvariantViolation(
                "Repository cannot cross its tenant scope",
                code="tenant_scope_mismatch",
            )

    async def claim_next(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> CollectorLease | None:
        self._assert_tenant(tenant_id)
        now = await self._database_now()
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == tenant_id,
                ShadowCampaignRecord.id == campaign_id,
            )
            .with_for_update()
        )
        if campaign is None or campaign.status not in _COLLECTABLE_CAMPAIGN_STATUSES:
            return None
        active = await self._session.scalar(
            select(func.count())
            .select_from(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                ShadowRunResultRecord.collector_lease_owner.is_not(None),
                ShadowRunResultRecord.collector_lease_expires_at > now,
            )
        )
        if int(active or 0) >= campaign.max_concurrency:
            return None
        result = await self._session.scalar(
            select(ShadowRunResultRecord)
            .join(
                SearchRunRecord,
                (SearchRunRecord.tenant_id == ShadowRunResultRecord.tenant_id)
                & (SearchRunRecord.id == ShadowRunResultRecord.search_run_id),
            )
            .where(
                ShadowRunResultRecord.tenant_id == tenant_id,
                ShadowRunResultRecord.campaign_id == campaign_id,
                ShadowRunResultRecord.scheduling_state == SchedulingState.SUBMITTED.value,
                ShadowRunResultRecord.reservation_state.in_(("ACTIVE", "SETTLED")),
                or_(
                    ShadowRunResultRecord.collector_lease_owner.is_(None),
                    ShadowRunResultRecord.collector_lease_expires_at <= now,
                ),
                SearchRunRecord.status.in_(_TERMINAL_RUN_STATUSES),
            )
            .order_by(ShadowRunResultRecord.schedule_ordinal)
            .with_for_update(skip_locked=True)
        )
        if result is None:
            return None
        cost_rate = self._cost_rate(campaign)
        expires_at = now + lease_duration
        result.collector_lease_owner = worker_id
        result.collector_lease_expires_at = expires_at
        result.collector_attempt_count += 1
        result.version += 1
        result.updated_at = now
        await self._session.flush()
        if result.conversation_id is None or result.search_run_id is None:
            raise InvariantViolation(
                "Submitted Result has no source binding",
                code="collector_source_binding_missing",
            )
        return CollectorLease(
            id=result.id,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            case_id=result.case_id,
            repetition=result.repetition,
            conversation_id=result.conversation_id,
            search_run_id=result.search_run_id,
            lease_owner=worker_id,
            lease_expires_at=expires_at,
            collector_schema_version=campaign.collector_schema_version,
            manifest_version=campaign.manifest_version,
            manifest_hash=campaign.manifest_hash,
            cost_rate=cost_rate,
            retention_until=result.retention_until,
            version=result.version,
            _persisted_version=result.version,
        )

    async def renew(
        self,
        lease: CollectorLease,
        lease_duration: timedelta,
    ) -> None:
        now, _, result = await self._locked_records(lease)
        self._assert_active_lease(result, lease, now)
        expires_at = now + lease_duration
        result.collector_lease_expires_at = expires_at
        result.version += 1
        result.updated_at = now
        await self._session.flush()
        lease.renew(expires_at, result.version)

    async def persist(
        self,
        lease: CollectorLease,
        outcome: CollectionOutcome,
    ) -> CollectionReceipt:
        now, campaign, result = await self._locked_records(lease)
        if result.scheduling_state == SchedulingState.COLLECTED.value:
            if not self._matches(result, outcome):
                raise InvariantViolation(
                    "Collected Result conflicts with the source snapshot",
                    code="collection_conflict",
                )
            await self._verify_gold_assertions(result.id, outcome)
            return CollectionReceipt(
                result.id,
                outcome.source_snapshot_digest,
                True,
                result.budget_violation,
            )
        self._assert_active_lease(result, lease, now)
        if (
            campaign.collector_schema_version != outcome.collector_schema_version
            or result.search_run_id != lease.search_run_id
        ):
            raise InvariantViolation(
                "Collector frozen inputs no longer match the Result",
                code="collector_snapshot_mismatch",
            )

        result.source_terminal_at = outcome.source_terminal_at
        settlement = await SqlShadowCampaignRepository(
            self._session,
            lease.tenant_id,
        ).settle_run_budget(
            lease.tenant_id,
            lease.campaign_id,
            lease.id,
            outcome.source_snapshot_digest,
            outcome.usage,
        )
        signal_flags = set(outcome.error_signal_flags)
        error_class = outcome.error_class
        error_code = outcome.error_code
        if settlement.budget_violation:
            signal_flags.add("budget_violation")
            error_class = ErrorClass.CANDIDATE_DEFECT
            error_code = "budget_violation"

        result.actual_mode = outcome.actual_mode
        result.run_status = outcome.run_status
        result.answer_quality = outcome.answer_quality
        result.run_stop_reason = outcome.run_stop_reason
        result.latency_ms = outcome.latency_ms
        result.minimum_required_facts = outcome.minimum_required_facts
        result.fact_total = outcome.fact_total
        result.fact_covered = outcome.fact_covered
        result.fact_gap = outcome.fact_gap
        result.plan_completeness_failure = outcome.plan_completeness_failure
        result.factual_claim_count = outcome.factual_claim_count
        result.nonfactual_claim_count = outcome.nonfactual_claim_count
        result.cited_factual_claim_count = outcome.cited_factual_claim_count
        result.valid_citation_chain_count = outcome.valid_citation_chain_count
        result.traceability_violation_count = outcome.traceability_violation_count
        result.gold_assertion_total = outcome.gold_assertion_total
        result.gold_assertion_passed = outcome.gold_assertion_passed
        result.gold_assertion_failed = outcome.gold_assertion_failed
        result.gold_assertion_not_applicable = outcome.gold_assertion_not_applicable
        result.oracle_version = outcome.oracle_version
        result.query_pollution_count = outcome.query_pollution_count
        result.model_call_count = outcome.model_call_count
        result.degraded = outcome.degraded
        result.provider_success_count = outcome.provider_success_count
        result.provider_failure_count = outcome.provider_failure_count
        result.error_class = error_class.value if error_class is not None else None
        result.error_code = error_code
        result.failed_phase = outcome.failed_phase
        result.error_signal_flags = sorted(signal_flags)
        result.scheduling_state = SchedulingState.COLLECTED.value
        result.collector_lease_owner = None
        result.collector_lease_expires_at = None
        result.collected_at = now
        result.collector_schema_version = outcome.collector_schema_version
        result.version += 1
        result.updated_at = now

        for assertion in outcome.gold_assertions:
            assertion_id = uuid5(result.id, f"gold-assertion:{assertion.assertion_id}")
            statement = (
                insert(ShadowGoldAssertionResultRecord)
                .values(
                    id=assertion_id,
                    tenant_id=lease.tenant_id,
                    campaign_id=lease.campaign_id,
                    result_id=result.id,
                    assertion_id=assertion.assertion_id,
                    critical=assertion.critical,
                    status=assertion.status.value,
                    reason_code=assertion.reason_code,
                    created_at=now,
                    retention_until=result.retention_until,
                )
                .on_conflict_do_nothing()
            )
            await self._session.execute(statement)

        await self._verify_gold_assertions(result.id, outcome)

        campaign.collected_count += 1
        if outcome.degraded:
            campaign.degraded_count += 1
        if campaign.collected_count > campaign.max_runs:
            raise InvariantViolation(
                "Campaign collected counter exceeds max_runs",
                code="campaign_ledger_mismatch",
            )
        campaign.version += 1
        campaign.updated_at = now
        await self._session.flush()
        return CollectionReceipt(
            result.id,
            outcome.source_snapshot_digest,
            False,
            settlement.budget_violation,
        )

    async def _verify_gold_assertions(
        self,
        result_id: UUID,
        outcome: CollectionOutcome,
    ) -> None:
        rows = (
            await self._session.execute(
                select(
                    ShadowGoldAssertionResultRecord.assertion_id,
                    ShadowGoldAssertionResultRecord.critical,
                    ShadowGoldAssertionResultRecord.status,
                    ShadowGoldAssertionResultRecord.reason_code,
                ).where(
                    ShadowGoldAssertionResultRecord.tenant_id == self._tenant_id,
                    ShadowGoldAssertionResultRecord.result_id == result_id,
                )
            )
        ).all()
        actual = {
            (row.assertion_id, row.critical, row.status, row.reason_code)
            for row in rows
        }
        expected = {
            (
                item.assertion_id,
                item.critical,
                item.status.value,
                item.reason_code,
            )
            for item in outcome.gold_assertions
        }
        if actual != expected or len(rows) != len(expected):
            raise InvariantViolation(
                "Gold assertion audit rows conflict with Collector output",
                code="gold_assertion_audit_mismatch",
            )

    async def _locked_records(
        self,
        lease: CollectorLease,
    ) -> tuple[datetime, ShadowCampaignRecord, ShadowRunResultRecord]:
        self._assert_tenant(lease.tenant_id)
        now = await self._database_now()
        campaign = await self._session.scalar(
            select(ShadowCampaignRecord)
            .where(
                ShadowCampaignRecord.tenant_id == lease.tenant_id,
                ShadowCampaignRecord.id == lease.campaign_id,
            )
            .with_for_update()
        )
        if campaign is None:
            raise InvariantViolation("Campaign is missing", code="campaign_not_found")
        result = await self._session.scalar(
            select(ShadowRunResultRecord)
            .where(
                ShadowRunResultRecord.tenant_id == lease.tenant_id,
                ShadowRunResultRecord.campaign_id == lease.campaign_id,
                ShadowRunResultRecord.id == lease.id,
            )
            .with_for_update()
        )
        if result is None:
            raise InvariantViolation(
                "Campaign Result is missing",
                code="campaign_result_not_found",
            )
        return now, campaign, result

    @staticmethod
    def _assert_active_lease(
        result: ShadowRunResultRecord,
        lease: CollectorLease,
        now: datetime,
    ) -> None:
        if (
            result.scheduling_state != SchedulingState.SUBMITTED.value
            or result.collector_lease_owner != lease.lease_owner
            or result.collector_lease_expires_at is None
            or result.collector_lease_expires_at <= now
            or result.version != lease.persisted_version
        ):
            raise InvariantViolation(
                "Collector lease fencing token is stale",
                code="collector_lease_fence_lost",
            )

    async def _database_now(self) -> datetime:
        now = await self._session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise InvariantViolation("Database clock was unavailable")
        return now

    @staticmethod
    def _cost_rate(campaign: ShadowCampaignRecord) -> CostRate:
        snapshot = campaign.cost_rate_snapshot
        try:
            cost_rate = CostRate(
                version=str(snapshot["version"]),
                prompt_per_million_usd=Decimal(str(snapshot["prompt_per_million_usd"])),
                completion_per_million_usd=Decimal(
                    str(snapshot["completion_per_million_usd"])
                ),
                possibly_billed_run_reserve_usd=Decimal(
                    str(snapshot["possibly_billed_run_reserve_usd"])
                ),
                run_reservation_usd=(
                    Decimal(str(snapshot["run_reservation_usd"]))
                    if "run_reservation_usd" in snapshot
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            raise InvariantViolation(
                "Campaign cost rate snapshot is invalid",
                code="campaign_cost_rate_invalid",
            ) from error
        if (
            cost_rate.version != campaign.cost_rate_version
            or cost_rate.sha256 != campaign.cost_rate_hash
        ):
            raise InvariantViolation(
                "Campaign cost rate snapshot hash is invalid",
                code="campaign_cost_rate_invalid",
            )
        return cost_rate

    @staticmethod
    def _matches(result: ShadowRunResultRecord, outcome: CollectionOutcome) -> bool:
        return bool(
            result.source_snapshot_digest == outcome.source_snapshot_digest
            and result.collector_schema_version == outcome.collector_schema_version
            and result.source_terminal_at == outcome.source_terminal_at
            and result.actual_mode == outcome.actual_mode
            and result.run_status == outcome.run_status
            and result.answer_quality == outcome.answer_quality
            and result.latency_ms == outcome.latency_ms
            and result.fact_total == outcome.fact_total
            and result.fact_covered == outcome.fact_covered
            and result.traceability_violation_count
            == outcome.traceability_violation_count
            and result.gold_assertion_total == outcome.gold_assertion_total
            and result.query_pollution_count == outcome.query_pollution_count
            and result.model_call_count == outcome.model_call_count
        )


__all__ = ["SqlShadowCollectorRepository", "SqlShadowSnapshotReader"]
