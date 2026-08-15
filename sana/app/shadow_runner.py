"""Crash-recoverable orchestration for a bounded Shadow Campaign."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sana.app.shadow_api_client import ShadowAPIError
from sana.modules.identity.domain import Principal
from sana.modules.shadow_campaign.domain import (
    CampaignStatus,
    ErrorClass,
    ReservationState,
    ReviewActor,
    ReviewVerdict,
    StopIntent,
)
from sana.modules.shadow_campaign.review import (
    HUMAN_REVIEW_REASON_CODES,
    ReviewScore,
    ReviewSubmission,
)
from sana.modules.shadow_campaign.runner import (
    CampaignReviewCandidate,
    CampaignRunState,
    CampaignRunSummary,
    RunnerFailure,
)
from sana.modules.shared.errors import InvariantViolation


_TRANSIENT_COLLECTOR_CODES = frozenset(
    {"source_not_sealed", "source_outbox_unpublished"}
)

if TYPE_CHECKING:
    from sana.app.shadow_collector import ShadowCollectorService
    from sana.app.shadow_report import CampaignReportResult, ShadowReportService
    from sana.app.shadow_review import ShadowReviewService
    from sana.modules.shadow_campaign.budget import CampaignBudgetService
    from sana.modules.shadow_campaign.execution import CampaignExecutionService
    from sana.modules.shadow_campaign.manifest import ShadowManifest
    from sana.modules.shadow_campaign.ports import CampaignUnitOfWorkFactory
    from sana.modules.shadow_campaign.scheduler import (
        CampaignSchedulingService,
        RunLease,
    )
    from sana.modules.shadow_campaign.service import (
        CampaignCreationReceipt,
        CampaignLifecycleService,
        CampaignService,
        CreateCampaignCommand,
    )
    from sana.modules.shared.clock import Clock


@dataclass(frozen=True, slots=True)
class ReviewBatchReceipt:
    campaign_id: UUID
    selected: int
    already_reviewed: int
    human_reviewed: int
    system_reviewed: int


class InteractiveShadowReview:
    """Show ephemeral evidence while persisting only fixed structured verdicts."""

    def __init__(
        self,
        runner: "ShadowCampaignRunner",
        reviews: "ShadowReviewService",
        *,
        reader: Callable[[str], str] = input,
        writer: Callable[[str], None] = print,
    ) -> None:
        self._runner = runner
        self._reviews = reviews
        self._reader = reader
        self._writer = writer

    async def review(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> ReviewBatchReceipt:
        candidates = await self._runner.review_candidates(principal, campaign_id)
        existing = human = system = 0
        for candidate in candidates:
            if candidate.reviewed:
                existing += 1
                continue
            if (
                candidate.answerability == "answerable"
                and candidate.answer_quality not in {"COMPLETE", "PARTIAL"}
            ):
                await self._reviews.record_system(
                    ReviewSubmission.expected_answer_missing(
                        tenant_id=principal.tenant_id,
                        campaign_id=campaign_id,
                        result_id=candidate.result_id,
                        rubric_version=candidate.rubric_version,
                    )
                )
                system += 1
                continue
            try:
                projection = await self._reviews.projection(
                    principal,
                    campaign_id,
                    candidate.result_id,
                )
            except InvariantViolation:
                await self._reviews.record_system(
                    ReviewSubmission.material_unavailable(
                        tenant_id=principal.tenant_id,
                        campaign_id=campaign_id,
                        result_id=candidate.result_id,
                        rubric_version=candidate.rubric_version,
                    )
                )
                system += 1
                continue
            if projection is None:
                raise InvariantViolation(
                    "Review projection lost owner authorization",
                    code="campaign_owner_binding_lost",
                )
            self._show_projection(projection)
            await self._reviews.submit_human(
                principal,
                self._read_submission(principal, campaign_id, candidate),
            )
            human += 1
        return ReviewBatchReceipt(
            campaign_id,
            len(candidates),
            existing,
            human,
            system,
        )

    def _show_projection(self, projection) -> None:
        self._writer(
            f"Result {projection.result_id} | case={projection.case_id} "
            f"repetition={projection.repetition}"
        )
        self._writer(f"Answer:\n{projection.answer_text}")
        for index, claim in enumerate(projection.claims, start=1):
            self._writer(
                f"Claim {index} [{claim.claim_kind}/{claim.support_status}]: "
                f"{claim.claim_text}"
            )
            for citation in claim.citations:
                self._writer(
                    f"  {citation.label} {citation.rendered_url}\n"
                    f"  quote: {citation.quote}"
                )

    def _read_submission(
        self,
        principal: Principal,
        campaign_id: UUID,
        candidate: CampaignReviewCandidate,
    ) -> ReviewSubmission:
        verdict = self._choice(
            "Correctness [correct/minor/major/unreviewable]: ",
            {
                "correct": ReviewVerdict.CORRECT,
                "minor": ReviewVerdict.MINOR_ERROR,
                "major": ReviewVerdict.MAJOR_ERROR,
                "unreviewable": ReviewVerdict.UNREVIEWABLE,
            },
        )
        if verdict is ReviewVerdict.UNREVIEWABLE:
            citation = source = freshness = completeness = ReviewScore.NOT_APPLICABLE
        else:
            scores = {
                "pass": ReviewScore.PASS,
                "fail": ReviewScore.FAIL,
                "na": ReviewScore.NOT_APPLICABLE,
            }
            citation = self._choice("Citation relevance [pass/fail/na]: ", scores)
            source = self._choice("Source appropriateness [pass/fail/na]: ", scores)
            freshness = self._choice("Freshness [pass/fail/na]: ", scores)
            completeness = self._choice("Completeness [pass/fail/na]: ", scores)
        raw_reasons = self._reader(
            "Reason codes (comma-separated allowlist, blank if correct): "
        )
        reasons = tuple(
            item.strip() for item in raw_reasons.split(",") if item.strip()
        )
        if any(item not in HUMAN_REVIEW_REASON_CODES for item in reasons):
            raise ValueError("Review reason code is not allowlisted")
        return ReviewSubmission(
            principal.tenant_id,
            campaign_id,
            candidate.result_id,
            candidate.rubric_version,
            verdict,
            citation,
            source,
            freshness,
            completeness,
            reasons,
            ReviewActor.HUMAN,
            principal.user_id,
        )

    def _choice(self, prompt: str, values: dict[str, object]):
        selected = self._reader(prompt).strip().lower()
        if selected not in values:
            raise ValueError("Review choice is invalid")
        return values[selected]


class ShadowCampaignRunner:
    """Coordinates services while persistence remains the recovery authority."""

    MAX_CONCURRENCY = 2

    def __init__(
        self,
        *,
        uow_factory: "CampaignUnitOfWorkFactory",
        campaigns: "CampaignService",
        lifecycle: "CampaignLifecycleService",
        scheduling: "CampaignSchedulingService",
        budget: "CampaignBudgetService",
        execution: "CampaignExecutionService",
        collector: "ShadowCollectorService",
        reports: "ShadowReportService",
        clock: "Clock",
        worker_id: str,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        normalized_worker = worker_id.strip()
        if not normalized_worker or len(normalized_worker) > 180:
            raise ValueError("Runner worker ID must contain between 1 and 180 characters")
        if poll_interval_seconds <= 0:
            raise ValueError("Runner poll interval must be positive")
        self._uow_factory = uow_factory
        self._campaigns = campaigns
        self._lifecycle = lifecycle
        self._scheduling = scheduling
        self._budget = budget
        self._execution = execution
        self._collector = collector
        self._reports = reports
        self._clock = clock
        self._worker_id = normalized_worker
        self._sleeper = sleeper
        self._poll_interval = poll_interval_seconds

    async def create(
        self,
        principal: Principal,
        command: "CreateCampaignCommand",
    ) -> tuple["CampaignCreationReceipt", "CampaignReportResult | None"]:
        if (
            command.tenant_id != principal.tenant_id
            or command.user_id != principal.user_id
        ):
            raise InvariantViolation(
                "Campaign command is not bound to the authenticated principal",
                code="campaign_principal_mismatch",
            )
        receipt = await self._campaigns.create(command)
        state = await self.state(principal, receipt.id)
        if state is None:
            raise InvariantViolation(
                "Created Campaign is not visible to its owner",
                code="campaign_owner_binding_lost",
            )
        if state.status is CampaignStatus.CREATED:
            materialized = await self._scheduling.materialize(
                principal.tenant_id,
                principal.user_id,
                receipt.id,
                command.manifest,
            )
            if materialized is None:
                raise InvariantViolation(
                    "Campaign materialization lost owner authorization",
                    code="campaign_owner_binding_lost",
                )
            started = await self._lifecycle.start(
                principal.tenant_id,
                principal.user_id,
                receipt.id,
            )
            if started is None:
                raise InvariantViolation(
                    "Campaign start lost owner authorization",
                    code="campaign_owner_binding_lost",
                )
        report = None
        state = await self.state(principal, receipt.id)
        if state is not None and state.status is CampaignStatus.RUNNING:
            report = await self.run(principal, receipt.id, command.manifest)
        return receipt, report

    async def list(self, principal: Principal) -> tuple[CampaignRunSummary, ...]:
        async with self._uow_factory(principal.tenant_id) as uow:
            return await uow.campaign_runner.list_owned(
                principal.tenant_id,
                principal.user_id,
            )

    async def state(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> CampaignRunState | None:
        async with self._uow_factory(principal.tenant_id) as uow:
            return await uow.campaign_runner.read_owned_state(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
            )

    async def review_candidates(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> tuple[CampaignReviewCandidate, ...]:
        async with self._uow_factory(principal.tenant_id) as uow:
            return await uow.campaign_runner.review_candidates(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
            )

    async def resume(
        self,
        principal: Principal,
        campaign_id: UUID,
        manifest: "ShadowManifest",
    ) -> "CampaignReportResult | None":
        state = await self._require_owned_state(principal, campaign_id)
        if state.status in {CampaignStatus.COMPLETED, CampaignStatus.ABORTED}:
            raise InvariantViolation(
                "A terminal Campaign cannot be resumed",
                code="terminal_campaign_resume",
            )
        if state.status is CampaignStatus.AWAITING_REVIEW:
            raise InvariantViolation(
                "A Campaign awaiting review cannot resume scheduling",
                code="campaign_awaiting_review",
            )
        if state.status is CampaignStatus.CREATED:
            raise InvariantViolation(
                "A CREATED Campaign must be recovered through its create key",
                code="campaign_not_started",
            )
        if state.status is CampaignStatus.PAUSED:
            resumed = await self._lifecycle.resume(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
            )
            if resumed is None:
                raise InvariantViolation(
                    "Campaign resume lost owner authorization",
                    code="campaign_owner_binding_lost",
                )
        return await self.run(principal, campaign_id, manifest)

    async def pause(
        self,
        principal: Principal,
        campaign_id: UUID,
        manifest: "ShadowManifest",
    ) -> CampaignRunState:
        state = await self._require_owned_state(principal, campaign_id)
        if state.status is CampaignStatus.PAUSED:
            return state
        if state.status is CampaignStatus.STOPPING:
            if state.stop_intent is not StopIntent.PAUSE:
                raise InvariantViolation(
                    "A terminal Campaign stop cannot be downgraded to pause",
                    code="campaign_pause_state_invalid",
                )
            await self.run(principal, campaign_id, manifest)
            return await self._require_owned_state(principal, campaign_id)
        if state.status is not CampaignStatus.RUNNING:
            raise InvariantViolation(
                "Only a RUNNING Campaign can be paused",
                code="campaign_pause_state_invalid",
            )
        await self._lifecycle.request_stop(
            principal.tenant_id,
            principal.user_id,
            campaign_id,
            StopIntent.PAUSE,
            "operator_pause",
        )
        await self.run(principal, campaign_id, manifest)
        return await self._require_owned_state(principal, campaign_id)

    async def abort(
        self,
        principal: Principal,
        campaign_id: UUID,
        manifest: "ShadowManifest",
    ) -> CampaignRunState:
        state = await self._require_owned_state(principal, campaign_id)
        if state.status is CampaignStatus.ABORTED:
            return state
        if state.status is CampaignStatus.COMPLETED:
            raise InvariantViolation(
                "Campaign cannot be aborted from its current state",
                code="campaign_abort_state_invalid",
            )
        if state.status in {
            CampaignStatus.CREATED,
            CampaignStatus.PAUSED,
            CampaignStatus.AWAITING_REVIEW,
        }:
            await self._lifecycle.abort(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
                "operator_abort",
            )
        elif state.status is CampaignStatus.RUNNING:
            await self._lifecycle.request_stop(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
                StopIntent.ABORT,
                "operator_abort",
            )
        elif (
            state.status is CampaignStatus.STOPPING
            and state.stop_intent is StopIntent.PAUSE
        ):
            await self._lifecycle.escalate_stop(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
                StopIntent.ABORT,
                "operator_abort",
            )
        current = await self._require_owned_state(principal, campaign_id)
        if current.status is CampaignStatus.STOPPING:
            await self.run(principal, campaign_id, manifest)
        return await self._require_owned_state(principal, campaign_id)

    async def report(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> "CampaignReportResult | None":
        await self._require_owned_state(principal, campaign_id)
        return await self._reports.generate(principal, campaign_id)

    async def run(
        self,
        principal: Principal,
        campaign_id: UUID,
        manifest: "ShadowManifest",
    ) -> "CampaignReportResult | None":
        try:
            return await self._run_loop(principal, campaign_id, manifest)
        except asyncio.CancelledError:
            await asyncio.shield(self._pause_on_interrupt(principal, campaign_id))
            raise

    async def _run_loop(
        self,
        principal: Principal,
        campaign_id: UUID,
        manifest: "ShadowManifest",
    ) -> "CampaignReportResult | None":
        tasks: set[asyncio.Task[None]] = set()
        while True:
            await self._collect_available(principal, campaign_id, manifest)
            done = {task for task in tasks if task.done()}
            tasks.difference_update(done)
            for task in done:
                task.result()

            state = await self._require_owned_state(principal, campaign_id)
            if state.status in {
                CampaignStatus.COMPLETED,
                CampaignStatus.ABORTED,
                CampaignStatus.PAUSED,
                CampaignStatus.AWAITING_REVIEW,
            }:
                if tasks:
                    await asyncio.gather(*tasks)
                    tasks.clear()
                    continue
                return await self._reports.generate(principal, campaign_id)

            while len(tasks) < self.MAX_CONCURRENCY:
                lease = await self._scheduling.claim_next(
                    principal.tenant_id,
                    campaign_id,
                    f"{self._worker_id}:scheduler",
                )
                if lease is None:
                    break
                tasks.add(
                    asyncio.create_task(
                        self._submit(principal, lease, manifest),
                        name=f"shadow-submit-{lease.schedule_ordinal}",
                    )
                )

            if tasks:
                await asyncio.wait(
                    tasks,
                    timeout=self._poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                continue

            state = await self._require_owned_state(principal, campaign_id)
            if state.status is CampaignStatus.STOPPING:
                if not state.has_inflight_work:
                    if state.stop_intent is not StopIntent.PAUSE:
                        await self._skip_pending(
                            principal.tenant_id,
                            campaign_id,
                            f"campaign_{state.stop_intent.value.lower()}",
                        )
                    await self._lifecycle.settle_stop(
                        principal.tenant_id,
                        campaign_id,
                    )
                else:
                    await self._sleeper(self._poll_interval)
                continue
            if state.execution_sealed:
                prepared = await self._reports.generate(principal, campaign_id)
                if prepared is not None and not prepared.final:
                    await self._lifecycle.await_review(
                        principal.tenant_id,
                        campaign_id,
                        self._clock.now() + timedelta(hours=48),
                    )
                    return await self._reports.generate(principal, campaign_id)
                return prepared
            await self._sleeper(self._poll_interval)

    async def _pause_on_interrupt(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> None:
        state = await self._require_owned_state(principal, campaign_id)
        if state.status is CampaignStatus.RUNNING:
            await self._lifecycle.request_stop(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
                StopIntent.PAUSE,
                "operator_interrupt",
            )

    async def _submit(
        self,
        principal: Principal,
        lease: "RunLease",
        manifest: "ShadowManifest",
    ) -> None:
        case = next((item for item in manifest.cases if item.id == lease.case_id), None)
        if case is None:
            await self._fail_and_stop(
                principal,
                lease,
                RunnerFailure(
                    ErrorClass.PERMANENT_CONFIGURATION,
                    "manifest_case_missing",
                    "manifest_lookup",
                    False,
                ),
            )
            return
        if lease.reservation_state is ReservationState.NONE:
            admission = await self._budget.reserve_run(lease)
            if not admission.allowed:
                await self._mark_failure(
                    lease,
                    RunnerFailure(
                        ErrorClass.PERMANENT_CONFIGURATION,
                        "budget_admission_denied",
                        "budget_admission",
                        False,
                    ),
                )
                return
        try:
            await self._execution.execute(lease, case.prompt)
        except ShadowAPIError as error:
            failure = RunnerFailure(
                ErrorClass.PROVIDER_TRANSIENT
                if error.retryable
                else ErrorClass.PERMANENT_CONFIGURATION,
                error.code,
                "candidate_api",
                error.request_may_have_committed,
            )
            await self._ensure_fatal_stop(
                principal,
                lease.campaign_id,
                failure.error_code,
            )
            current_attempts = (
                lease.submission_attempt_count
                if lease.state.value == "CONVERSATION_BOUND"
                else lease.conversation_attempt_count
            )
            if not error.request_may_have_committed or current_attempts > 1:
                await self._mark_failure(lease, failure)
        except InvariantViolation:
            await self._fail_and_stop(
                principal,
                lease,
                RunnerFailure(
                    ErrorClass.PERMANENT_CONFIGURATION,
                    "runner_invariant_violation",
                    "candidate_binding",
                    False,
                ),
            )

    async def _collect_available(
        self,
        principal: Principal,
        campaign_id: UUID,
        manifest: "ShadowManifest",
    ) -> int:
        collected = 0
        while True:
            lease = await self._collector.claim_next(
                principal.tenant_id,
                campaign_id,
                f"{self._worker_id}:collector",
            )
            if lease is None:
                return collected
            for attempt in range(4):
                try:
                    await self._collector.collect(lease, manifest)
                    collected += 1
                    break
                except InvariantViolation as error:
                    if error.code not in _TRANSIENT_COLLECTOR_CODES:
                        await self._seal_collector_failure(
                            principal,
                            lease,
                            RunnerFailure(
                                ErrorClass.PERMANENT_CONFIGURATION,
                                error.code,
                                "collector_validation",
                                False,
                            ),
                        )
                        break
                    if attempt == 3:
                        await self._seal_collector_failure(
                            principal,
                            lease,
                            RunnerFailure(
                                ErrorClass.INFRASTRUCTURE,
                                "collector_retry_exhausted",
                                "collector_validation",
                                False,
                            ),
                        )
                        break
                    await self._sleeper((0.5, 1.0, 2.0)[attempt])
                except Exception:
                    if attempt == 3:
                        await self._seal_collector_failure(
                            principal,
                            lease,
                            RunnerFailure(
                                ErrorClass.INFRASTRUCTURE,
                                "collector_retry_exhausted",
                                "collector_read",
                                False,
                            ),
                        )
                        break
                    await self._sleeper((0.5, 1.0, 2.0)[attempt])

    async def _fail_and_stop(
        self,
        principal: Principal,
        lease: "RunLease",
        failure: RunnerFailure,
    ) -> None:
        await self._ensure_fatal_stop(
            principal,
            lease.campaign_id,
            failure.error_code,
        )
        await self._mark_failure(lease, failure)

    async def _seal_collector_failure(
        self,
        principal: Principal,
        lease,
        failure: RunnerFailure,
    ) -> None:
        await self._ensure_fatal_stop(
            principal,
            lease.campaign_id,
            failure.error_code,
        )
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaign_runner.mark_collector_failure(lease, failure)
            await uow.commit()

    async def _ensure_fatal_stop(
        self,
        principal: Principal,
        campaign_id: UUID,
        reason: str,
    ) -> None:
        state = await self._require_owned_state(principal, campaign_id)
        if state.status is not CampaignStatus.RUNNING:
            return
        try:
            await self._lifecycle.request_stop(
                principal.tenant_id,
                principal.user_id,
                campaign_id,
                StopIntent.FATAL,
                reason[:200],
            )
        except InvariantViolation as error:
            if error.code != "illegal_state_transition":
                raise

    async def _mark_failure(self, lease: "RunLease", failure: RunnerFailure) -> None:
        async with self._uow_factory(lease.tenant_id) as uow:
            await uow.campaign_runner.mark_failure(lease, failure)
            await uow.commit()

    async def _skip_pending(
        self,
        tenant_id: UUID,
        campaign_id: UUID,
        reason: str,
    ) -> int:
        async with self._uow_factory(tenant_id) as uow:
            count = await uow.campaign_runner.skip_pending(
                tenant_id,
                campaign_id,
                reason,
            )
            await uow.commit()
            return count

    async def _require_owned_state(
        self,
        principal: Principal,
        campaign_id: UUID,
    ) -> CampaignRunState:
        state = await self.state(principal, campaign_id)
        if state is None:
            raise InvariantViolation(
                "Campaign was not found for the authenticated owner",
                code="campaign_not_found",
            )
        return state


__all__ = ["InteractiveShadowReview", "ReviewBatchReceipt", "ShadowCampaignRunner"]
