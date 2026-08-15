"""Consistent Campaign decision snapshots and fenced final report binding."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sana.modules.shadow_campaign.budget import SettlementUsage
from sana.modules.shadow_campaign.collector import (
    CollectionOutcome,
    CollectorLease,
    GoldAssertionResult,
    GoldAssertionStatus,
    source_snapshot_digest,
)
from sana.modules.shadow_campaign.domain import CampaignStatus, ErrorClass, GateStatus
from sana.modules.shadow_campaign.policy import CostRate
from sana.modules.shadow_campaign.report import (
    CampaignReportSnapshot,
    DECISION_INPUT_SCHEMA_VERSION,
    FinalReportBinding,
    FinalReportReceipt,
)
from sana.modules.shared.errors import InvariantViolation
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.shadow_campaign import (
    ShadowCampaignRecord,
    ShadowGoldAssertionResultRecord,
    ShadowManualReviewRecord,
    ShadowRunResultRecord,
)
from sana.platform.db.shadow_collector import SqlShadowSnapshotReader


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SqlShadowReportGateway:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        snapshot_reader: SqlShadowSnapshotReader | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._snapshot_reader = snapshot_reader or SqlShadowSnapshotReader(
            session_factory
        )

    async def read(
        self,
        tenant_id: UUID,
        user_id: UUID,
        campaign_id: UUID,
    ) -> CampaignReportSnapshot | None:
        session = self._session_factory()
        try:
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await self._set_tenant(session, tenant_id)
            campaign = await session.scalar(
                select(ShadowCampaignRecord).where(
                    ShadowCampaignRecord.tenant_id == tenant_id,
                    ShadowCampaignRecord.id == campaign_id,
                )
            )
            if campaign is None or campaign.created_by_user_id != user_id:
                return None
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise InvariantViolation("Database clock was unavailable")
            return await self._snapshot(session, campaign, now)
        finally:
            await session.rollback()
            await session.close()

    async def bind(self, binding: FinalReportBinding) -> FinalReportReceipt:
        self._validate_binding(binding)
        session = self._session_factory()
        try:
            async with session.begin():
                await self._set_tenant(session, binding.tenant_id)
                now = await session.scalar(select(func.clock_timestamp()))
                if now is None:
                    raise InvariantViolation("Database clock was unavailable")
                campaign = await session.scalar(
                    select(ShadowCampaignRecord)
                    .where(
                        ShadowCampaignRecord.tenant_id == binding.tenant_id,
                        ShadowCampaignRecord.id == binding.campaign_id,
                    )
                    .with_for_update()
                )
                if campaign is None:
                    raise InvariantViolation(
                        "Campaign is missing",
                        code="campaign_not_found",
                    )
                if campaign.created_by_user_id != binding.owner_user_id:
                    raise InvariantViolation(
                        "Only the Campaign owner may bind its report",
                        code="report_owner_mismatch",
                    )
                existing = self._existing_receipt(campaign, binding)
                if existing is not None:
                    return existing
                if (
                    campaign.status != binding.expected_campaign_status.value
                    or campaign.version != binding.expected_campaign_version
                ):
                    raise InvariantViolation(
                        "Campaign changed after report artifacts were prepared",
                        code="report_input_stale",
                    )
                current = await self._snapshot(session, campaign, now)
                if current.decision_input_hash != binding.decision_input_hash:
                    raise InvariantViolation(
                        "Campaign decision input changed before report binding",
                        code="report_input_stale",
                    )
                if (
                    binding.finalization_reason == "review_deadline_expired"
                    and (
                        campaign.review_deadline_at is None
                        or now < campaign.review_deadline_at
                    )
                ):
                    raise InvariantViolation(
                        "Review deadline has not expired",
                        code="report_input_stale",
                    )

                target_status = self._terminal_status(campaign)
                campaign.status = target_status.value
                campaign.gate_status = binding.gate_status.value
                campaign.automatic_gate_status = binding.automatic_gate_status
                campaign.manual_review_status = binding.manual_review_status
                campaign.final_json_uri = binding.json_uri
                campaign.final_json_sha256 = binding.json_sha256
                campaign.final_markdown_uri = binding.markdown_uri
                campaign.final_markdown_sha256 = binding.markdown_sha256
                campaign.decision_input_hash = binding.decision_input_hash
                campaign.decision_hash = binding.decision_hash
                campaign.completed_at = campaign.completed_at or now
                campaign.version += 1
                campaign.updated_at = now
                await session.flush()
                return FinalReportReceipt(
                    campaign.id,
                    binding.gate_status,
                    binding.decision_hash,
                    binding.json_uri,
                    binding.json_sha256,
                    binding.markdown_uri,
                    binding.markdown_sha256,
                    False,
                )
        finally:
            await session.close()

    async def _snapshot(
        self,
        session: AsyncSession,
        campaign: ShadowCampaignRecord,
        now,
    ) -> CampaignReportSnapshot:
        results = tuple(
            (
                await session.scalars(
                    select(ShadowRunResultRecord)
                    .where(
                        ShadowRunResultRecord.tenant_id == campaign.tenant_id,
                        ShadowRunResultRecord.campaign_id == campaign.id,
                    )
                    .order_by(
                        ShadowRunResultRecord.schedule_ordinal,
                        ShadowRunResultRecord.id,
                    )
                )
            ).all()
        )
        reviews = tuple(
            (
                await session.scalars(
                    select(ShadowManualReviewRecord)
                    .where(
                        ShadowManualReviewRecord.tenant_id == campaign.tenant_id,
                        ShadowManualReviewRecord.campaign_id == campaign.id,
                    )
                    .order_by(
                        ShadowManualReviewRecord.result_id,
                        ShadowManualReviewRecord.id,
                    )
                )
            ).all()
        )
        gold = tuple(
            (
                await session.scalars(
                    select(ShadowGoldAssertionResultRecord)
                    .where(
                        ShadowGoldAssertionResultRecord.tenant_id
                        == campaign.tenant_id,
                        ShadowGoldAssertionResultRecord.campaign_id == campaign.id,
                    )
                    .order_by(
                        ShadowGoldAssertionResultRecord.result_id,
                        ShadowGoldAssertionResultRecord.assertion_id,
                    )
                )
            ).all()
        )
        run_ids = tuple(
            item.search_run_id for item in results if item.search_run_id is not None
        )
        invocations = (
            tuple(
                (
                    await session.scalars(
                        select(ModelInvocationRecord)
                        .where(
                            ModelInvocationRecord.tenant_id == campaign.tenant_id,
                            ModelInvocationRecord.run_id.in_(run_ids),
                        )
                        .order_by(
                            ModelInvocationRecord.run_id,
                            ModelInvocationRecord.id,
                        )
                    )
                ).all()
            )
            if run_ids
            else ()
        )
        gold_by_result: dict[UUID, list[ShadowGoldAssertionResultRecord]] = defaultdict(list)
        for item in gold:
            gold_by_result[item.result_id].append(item)
        current_digests = await self._source_digests(
            campaign,
            results,
            gold_by_result,
            now,
        )
        decision_input = self._decision_input(
            campaign,
            results,
            reviews,
            gold,
            invocations,
            current_digests,
        )
        existing = None
        if campaign.final_json_uri is not None:
            existing = {
                "gate_status": campaign.gate_status,
                "decision_input_hash": campaign.decision_input_hash,
                "decision_hash": campaign.decision_hash,
                "json_uri": campaign.final_json_uri,
                "json_sha256": campaign.final_json_sha256,
                "markdown_uri": campaign.final_markdown_uri,
                "markdown_sha256": campaign.final_markdown_sha256,
            }
        return CampaignReportSnapshot(
            campaign.tenant_id,
            campaign.id,
            campaign.created_by_user_id,
            CampaignStatus(campaign.status),
            campaign.version,
            now,
            campaign.review_deadline_at,
            decision_input,
            existing,
        )

    async def _source_digests(
        self,
        campaign: ShadowCampaignRecord,
        results: tuple[ShadowRunResultRecord, ...],
        gold_by_result: Mapping[UUID, list[ShadowGoldAssertionResultRecord]],
        now,
    ) -> dict[UUID, str | None]:
        cost_rate = self._cost_rate(campaign)
        values: dict[UUID, str | None] = {}
        for result in results:
            if result.scheduling_state != "COLLECTED":
                values[result.id] = None
                continue
            if (
                result.conversation_id is None
                or result.search_run_id is None
                or result.source_terminal_at is None
                or result.actual_mode is None
                or result.run_status is None
                or result.answer_quality is None
                or result.latency_ms is None
                or result.collector_schema_version is None
            ):
                values[result.id] = "INVALID:result_measurement_incomplete"
                continue
            try:
                lease = CollectorLease(
                    id=result.id,
                    tenant_id=result.tenant_id,
                    campaign_id=result.campaign_id,
                    case_id=result.case_id,
                    repetition=result.repetition,
                    conversation_id=result.conversation_id,
                    search_run_id=result.search_run_id,
                    lease_owner="report-source-revalidation",
                    lease_expires_at=now + timedelta(minutes=1),
                    collector_schema_version=campaign.collector_schema_version,
                    manifest_version=campaign.manifest_version,
                    manifest_hash=campaign.manifest_hash,
                    cost_rate=cost_rate,
                    retention_until=result.retention_until,
                    version=max(result.version, 1),
                    _persisted_version=max(result.version, 1),
                )
                snapshot = await self._snapshot_reader.read(lease)
                outcome = self._stored_outcome(
                    result,
                    tuple(gold_by_result.get(result.id, ())),
                )
                values[result.id] = source_snapshot_digest(snapshot, outcome)
            except InvariantViolation as error:
                values[result.id] = f"INVALID:{error.code}"
            except (TypeError, ValueError, InvalidOperation):
                values[result.id] = "INVALID:result_measurement_invalid"
        return values

    @staticmethod
    def _stored_outcome(
        result: ShadowRunResultRecord,
        gold: tuple[ShadowGoldAssertionResultRecord, ...],
    ) -> CollectionOutcome:
        assert result.source_terminal_at is not None
        assert result.actual_mode is not None
        assert result.run_status is not None
        assert result.answer_quality is not None
        assert result.latency_ms is not None
        assert result.collector_schema_version is not None
        error_class = ErrorClass(result.error_class) if result.error_class else None
        gold_values = tuple(
            GoldAssertionResult(
                item.assertion_id,
                item.critical,
                GoldAssertionStatus(item.status),
                item.reason_code,
            )
            for item in gold
        )
        return CollectionOutcome(
            source_snapshot_digest=result.source_snapshot_digest or "",
            collector_schema_version=result.collector_schema_version,
            source_terminal_at=result.source_terminal_at,
            actual_mode=result.actual_mode,
            run_status=result.run_status,
            answer_quality=result.answer_quality,
            run_stop_reason=result.run_stop_reason,
            latency_ms=result.latency_ms,
            minimum_required_facts=result.minimum_required_facts,
            fact_total=result.fact_total,
            fact_covered=result.fact_covered,
            fact_gap=result.fact_gap,
            plan_completeness_failure=result.plan_completeness_failure,
            factual_claim_count=result.factual_claim_count,
            nonfactual_claim_count=result.nonfactual_claim_count,
            cited_factual_claim_count=result.cited_factual_claim_count,
            valid_citation_chain_count=result.valid_citation_chain_count,
            traceability_violation_count=result.traceability_violation_count,
            gold_assertions=gold_values,
            oracle_version=result.oracle_version,
            query_pollution_count=result.query_pollution_count,
            model_call_count=result.model_call_count,
            usage=SettlementUsage(
                result.settled_observed_provider_calls,
                result.prompt_tokens,
                result.completion_tokens,
                Decimal(result.settled_observed_cost),
                result.possibly_billed_call_charge,
                Decimal(result.possibly_billed_cost_charge),
            ),
            degraded=result.degraded,
            provider_success_count=result.provider_success_count,
            provider_failure_count=result.provider_failure_count,
            error_class=error_class,
            error_code=result.error_code,
            failed_phase=result.failed_phase,
            error_signal_flags=tuple(result.error_signal_flags),
        )

    @staticmethod
    def _cost_rate(campaign: ShadowCampaignRecord) -> CostRate:
        value = campaign.cost_rate_snapshot
        try:
            rate = CostRate(
                version=str(value["version"]),
                prompt_per_million_usd=Decimal(
                    str(value["prompt_per_million_usd"])
                ),
                completion_per_million_usd=Decimal(
                    str(value["completion_per_million_usd"])
                ),
                possibly_billed_run_reserve_usd=Decimal(
                    str(value["possibly_billed_run_reserve_usd"])
                ),
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise InvariantViolation(
                "Frozen Campaign cost rate is invalid",
                code="campaign_cost_rate_invalid",
            ) from error
        if rate.sha256 != campaign.cost_rate_hash:
            raise InvariantViolation(
                "Frozen Campaign cost rate hash does not match",
                code="campaign_cost_rate_invalid",
            )
        return rate

    @staticmethod
    def _decision_input(
        campaign: ShadowCampaignRecord,
        results: tuple[ShadowRunResultRecord, ...],
        reviews: tuple[ShadowManualReviewRecord, ...],
        gold: tuple[ShadowGoldAssertionResultRecord, ...],
        invocations: tuple[ModelInvocationRecord, ...],
        current_digests: Mapping[UUID, str | None],
    ) -> dict[str, object]:
        campaign_payload = {
            "stop_intent": campaign.stop_intent,
            "stop_reason": campaign.stop_reason,
            "profile_version": campaign.profile_version,
            "profile_hash": campaign.profile_hash,
            "profile_snapshot": campaign.profile_snapshot,
            "gate_policy_version": campaign.gate_policy_version,
            "gate_policy_hash": campaign.gate_policy_hash,
            "gate_policy_snapshot": campaign.gate_policy_snapshot,
            "manifest_version": campaign.manifest_version,
            "manifest_hash": campaign.manifest_hash,
            "manifest_case_count": campaign.manifest_case_count,
            "repetitions": campaign.repetitions,
            "review_rubric_version": campaign.review_rubric_version,
            "review_rubric_hash": campaign.review_rubric_hash,
            "review_rubric_snapshot": campaign.review_rubric_snapshot,
            "cost_rate_version": campaign.cost_rate_version,
            "cost_rate_hash": campaign.cost_rate_hash,
            "cost_rate_snapshot": campaign.cost_rate_snapshot,
            "candidate_commit_sha": campaign.candidate_commit_sha,
            "candidate_source_clean": campaign.candidate_source_clean,
            "candidate_image_id": campaign.candidate_image_id,
            "candidate_oci_revision": campaign.candidate_oci_revision,
            "alembic_head": campaign.alembic_head,
            "candidate_config_hash": campaign.candidate_config_hash,
            "harness_commit_sha": campaign.harness_commit_sha,
            "harness_source_clean": campaign.harness_source_clean,
            "harness_fileset_hash": campaign.harness_fileset_hash,
            "collector_schema_version": campaign.collector_schema_version,
            "environment_identity_hash": campaign.environment_identity_hash,
            "environment_snapshot": campaign.environment_snapshot,
            "max_runs": campaign.max_runs,
            "max_concurrency": campaign.max_concurrency,
            "estimated_cost_stop_threshold": campaign.estimated_cost_stop_threshold,
            "provider_call_admission_ceiling": (
                campaign.provider_call_admission_ceiling
            ),
            "provider_call_structural_ceiling": (
                campaign.provider_call_structural_ceiling
            ),
            "review_deadline_at": campaign.review_deadline_at,
            "active_wall_clock_ms": campaign.active_wall_clock_ms,
            "counts": {
                "planned_count": campaign.planned_count,
                "submitted_count": campaign.submitted_count,
                "collected_count": campaign.collected_count,
                "failed_count": campaign.failed_count,
                "skipped_count": campaign.skipped_count,
                "degraded_count": campaign.degraded_count,
            },
            "ledger": {
                "observed_provider_calls": campaign.observed_provider_calls,
                "possibly_billed_call_charge": campaign.possibly_billed_call_charge,
                "reserved_provider_calls": campaign.reserved_provider_calls,
                "observed_prompt_tokens": campaign.observed_prompt_tokens,
                "observed_completion_tokens": campaign.observed_completion_tokens,
                "observed_estimated_cost": campaign.observed_estimated_cost,
                "possibly_billed_cost_charge": campaign.possibly_billed_cost_charge,
                "reserved_estimated_cost": campaign.reserved_estimated_cost,
                "possibly_billed_count": campaign.possibly_billed_count,
            },
        }
        result_payload = [
            {
                "result_id": item.id,
                "search_run_id": item.search_run_id,
                "case_id": item.case_id,
                "repetition": item.repetition,
                "schedule_ordinal": item.schedule_ordinal,
                "manual_review_selected": item.manual_review_selected,
                "locale": item.locale,
                "category": item.category,
                "answerability": item.answerability,
                "expected_mode": item.expected_mode,
                "scheduling_state": item.scheduling_state,
                "submission_request_hash": item.submission_request_hash,
                "actual_mode": item.actual_mode,
                "run_status": item.run_status,
                "answer_quality": item.answer_quality,
                "run_stop_reason": item.run_stop_reason,
                "latency_ms": item.latency_ms,
                "minimum_required_facts": item.minimum_required_facts,
                "fact_total": item.fact_total,
                "fact_covered": item.fact_covered,
                "fact_gap": item.fact_gap,
                "plan_completeness_failure": item.plan_completeness_failure,
                "factual_claim_count": item.factual_claim_count,
                "nonfactual_claim_count": item.nonfactual_claim_count,
                "cited_factual_claim_count": item.cited_factual_claim_count,
                "valid_citation_chain_count": item.valid_citation_chain_count,
                "traceability_violation_count": item.traceability_violation_count,
                "gold_assertion_total": item.gold_assertion_total,
                "gold_assertion_passed": item.gold_assertion_passed,
                "gold_assertion_failed": item.gold_assertion_failed,
                "gold_assertion_not_applicable": (
                    item.gold_assertion_not_applicable
                ),
                "oracle_version": item.oracle_version,
                "query_pollution_count": item.query_pollution_count,
                "model_call_count": item.model_call_count,
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens,
                "estimated_cost": item.estimated_cost,
                "degraded": item.degraded,
                "provider_success_count": item.provider_success_count,
                "provider_failure_count": item.provider_failure_count,
                "error_class": item.error_class,
                "error_code": item.error_code,
                "failed_phase": item.failed_phase,
                "error_signal_flags": tuple(item.error_signal_flags),
                "stable_skip_reason": item.stable_skip_reason,
                "reserved_provider_calls": item.reserved_provider_calls,
                "reserved_estimated_cost": item.reserved_estimated_cost,
                "reservation_state": item.reservation_state,
                "settled_observed_provider_calls": (
                    item.settled_observed_provider_calls
                ),
                "settled_observed_cost": item.settled_observed_cost,
                "possibly_billed_call_charge": item.possibly_billed_call_charge,
                "possibly_billed_cost_charge": item.possibly_billed_cost_charge,
                "budget_violation": item.budget_violation,
                "source_terminal_at": item.source_terminal_at,
                "source_snapshot_digest": item.source_snapshot_digest,
                "current_source_digest": current_digests.get(item.id),
                "collector_schema_version": item.collector_schema_version,
            }
            for item in results
        ]
        review_payload = [
            {
                "result_id": item.result_id,
                "rubric_version": item.rubric_version,
                "correctness_verdict": item.correctness_verdict,
                "citation_relevance": item.citation_relevance,
                "source_appropriateness": item.source_appropriateness,
                "freshness": item.freshness,
                "completeness": item.completeness,
                "reason_codes": tuple(item.reason_codes),
                "actor_type": item.actor_type,
            }
            for item in reviews
        ]
        gold_payload = [
            {
                "result_id": item.result_id,
                "assertion_id": item.assertion_id,
                "critical": item.critical,
                "status": item.status,
                "reason_code": item.reason_code,
            }
            for item in gold
        ]
        invocation_payload = [
            {
                "invocation_id": item.id,
                "run_id": item.run_id,
                "status": item.status,
                "billing_disposition": item.billing_disposition,
                "provider_called": item.provider_called,
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens,
                "started_at": item.started_at,
                "completed_at": item.completed_at,
                "error_category": item.error_category,
                "error_code": item.error_code,
            }
            for item in invocations
        ]
        return {
            "schema": DECISION_INPUT_SCHEMA_VERSION,
            "campaign_id": campaign.id,
            "campaign": campaign_payload,
            "results": result_payload,
            "reviews": review_payload,
            "gold_assertions": gold_payload,
            "model_invocations": invocation_payload,
        }

    @staticmethod
    async def _set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    @staticmethod
    def _validate_binding(binding: FinalReportBinding) -> None:
        if binding.gate_status is GateStatus.PENDING:
            raise ValueError("Final report binding requires a terminal gate status")
        if not binding.finalization_reason.strip():
            raise ValueError("Final report binding requires a finalization reason")
        for value, field_name in (
            (binding.decision_input_hash, "decision_input_hash"),
            (binding.decision_hash, "decision_hash"),
            (binding.json_sha256, "json_sha256"),
            (binding.markdown_sha256, "markdown_sha256"),
        ):
            if not _SHA256.fullmatch(value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        for uri, digest in (
            (binding.json_uri, binding.json_sha256),
            (binding.markdown_uri, binding.markdown_sha256),
        ):
            parsed = urlparse(uri)
            parts = tuple(part for part in parsed.path.split("/") if part)
            if (
                parsed.scheme != "campaign-artifact"
                or parsed.netloc != str(binding.tenant_id)
                or parts != (str(binding.campaign_id), digest)
                or parsed.params
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Final report reference does not match its Campaign scope"
                )

    @staticmethod
    def _existing_receipt(
        campaign: ShadowCampaignRecord,
        binding: FinalReportBinding,
    ) -> FinalReportReceipt | None:
        if campaign.final_json_uri is None:
            if any(
                value is not None
                for value in (
                    campaign.final_json_sha256,
                    campaign.final_markdown_uri,
                    campaign.final_markdown_sha256,
                    campaign.decision_input_hash,
                    campaign.decision_hash,
                )
            ):
                raise InvariantViolation(
                    "Campaign final report binding is partial",
                    code="report_binding_corrupt",
                )
            return None
        existing_values = (
            campaign.decision_input_hash,
            campaign.decision_hash,
            campaign.gate_status,
            campaign.final_json_uri,
            campaign.final_json_sha256,
            campaign.final_markdown_uri,
            campaign.final_markdown_sha256,
        )
        requested_values = (
            binding.decision_input_hash,
            binding.decision_hash,
            binding.gate_status.value,
            binding.json_uri,
            binding.json_sha256,
            binding.markdown_uri,
            binding.markdown_sha256,
        )
        if existing_values != requested_values:
            raise InvariantViolation(
                "Campaign already has a different final report",
                code="report_conflict",
            )
        return FinalReportReceipt(
            campaign.id,
            GateStatus(campaign.gate_status),
            binding.decision_hash,
            binding.json_uri,
            binding.json_sha256,
            binding.markdown_uri,
            binding.markdown_sha256,
            True,
        )

    @staticmethod
    def _terminal_status(campaign: ShadowCampaignRecord) -> CampaignStatus:
        if campaign.status == CampaignStatus.COMPLETED.value:
            raise InvariantViolation(
                "Completed Campaign is missing its final report binding",
                code="report_binding_corrupt",
            )
        if campaign.status == CampaignStatus.ABORTED.value:
            return CampaignStatus.ABORTED
        if campaign.stop_intent in {"ABORT", "FATAL", "BUDGET", "CALL_CEILING"}:
            return CampaignStatus.ABORTED
        return CampaignStatus.COMPLETED


__all__ = ["SqlShadowReportGateway"]
