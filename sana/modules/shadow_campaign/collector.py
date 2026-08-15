"""Deterministic, privacy-preserving collection from sealed SearchRun records."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Iterable
from uuid import UUID

from sana.modules.evidence.domain import SourceAuthority
from sana.modules.orchestration.domain import RunStatus, SearchMode, StepStatus
from sana.modules.shadow_campaign.budget import SettlementUsage
from sana.modules.shadow_campaign.domain import (
    ErrorClass,
    canonical_json_bytes,
    require_aware,
)
from sana.modules.shadow_campaign.evaluator import evaluate_gold_assertion
from sana.modules.shadow_campaign.manifest import Answerability, OracleType, ShadowCase
from sana.modules.shadow_campaign.policy import CostRate
from sana.modules.shared.errors import InvariantViolation


COLLECTOR_SCHEMA_VERSION = "shadow-collector-v2"

_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)
_TERMINAL_STEP_STATUSES = frozenset(
    {
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
    }
)
_TERMINAL_INVOCATION_STATUSES = frozenset(
    {"COMPLETED", "FAILED", "ABANDONED", "REUSED"}
)
_TERMINAL_PROVIDER_STATUSES = frozenset({"SUCCEEDED", "FAILED"})
_COVERED_FACT_STATUSES = frozenset({"COVERED", "VERIFIED"})
_VALID_CLAIM_KINDS = frozenset({"FACTUAL", "UNCERTAINTY", "COMMENTARY"})
_VALID_BILLING = frozenset({"NOT_BILLED", "BILLED", "POSSIBLY_BILLED"})
_MODEL_LIMIT = {SearchMode.FAST.value: 4, SearchMode.RESEARCH.value: 8}
_ERROR_CATEGORY = frozenset(
    {
        "TRANSIENT",
        "PERMANENT",
        "BUDGET",
        "CONTENT",
        "MODEL_OUTPUT",
        "CANCELLED",
        "INTERNAL",
        "INFRASTRUCTURE",
    }
)


class GoldAssertionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(slots=True)
class CollectorLease:
    id: UUID
    tenant_id: UUID
    campaign_id: UUID
    case_id: str
    repetition: int
    conversation_id: UUID
    search_run_id: UUID
    lease_owner: str
    lease_expires_at: datetime
    collector_schema_version: str
    manifest_version: str
    manifest_hash: str
    cost_rate: CostRate
    retention_until: datetime
    version: int
    _persisted_version: int

    def __post_init__(self) -> None:
        require_aware(self.lease_expires_at, "collector lease expiry")
        require_aware(self.retention_until, "collector retention")
        if not self.lease_owner.strip() or not self.collector_schema_version.strip():
            raise ValueError("Collector lease owner and schema version are required")
        if self.repetition < 1 or self.version < 1 or self._persisted_version != self.version:
            raise ValueError("Collector lease fencing state is invalid")
        if len(self.manifest_hash) != 64 or any(
            item not in "0123456789abcdef" for item in self.manifest_hash
        ):
            raise ValueError("Collector lease manifest hash is invalid")

    @property
    def persisted_version(self) -> int:
        return self._persisted_version

    def renew(self, expires_at: datetime, version: int) -> None:
        require_aware(expires_at, "collector lease expiry")
        if expires_at <= self.lease_expires_at or version <= self.version:
            raise InvariantViolation(
                "Collector lease renewal must advance expiry and fencing version",
                code="collector_lease_renewal_invalid",
            )
        self.lease_expires_at = expires_at
        self.version = version
        self._persisted_version = version


@dataclass(frozen=True, slots=True)
class CollectionReceipt:
    result_id: UUID
    source_snapshot_digest: str
    duplicate: bool
    budget_violation: bool


@dataclass(frozen=True, slots=True)
class GoldAssertionResult:
    assertion_id: str
    critical: bool
    status: GoldAssertionStatus
    reason_code: str


@dataclass(frozen=True, slots=True)
class SourceFact:
    id: UUID
    required: bool
    status: str
    freshness: str
    consequence: str


@dataclass(frozen=True, slots=True)
class SourceQuery:
    id: UUID
    fact_requirement_id: UUID | None
    plan_revision: int
    provider_class: str
    query_text: str


@dataclass(frozen=True, slots=True)
class SourceProviderAttempt:
    id: UUID
    query_spec_id: UUID
    provider: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class SourceStep:
    id: UUID
    step_key: str
    step_type: str
    plan_revision: int
    status: str
    output_bound: bool


@dataclass(frozen=True, slots=True)
class SourceAttempt:
    id: UUID
    step_id: UUID
    attempt_no: int
    started_at: datetime
    completed_at: datetime | None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class SourceInvocation:
    id: UUID
    step_id: UUID
    attempt_id: UUID
    role: str
    provider: str
    model: str
    call_no: int
    status: str
    billing_disposition: str
    provider_called: bool
    prompt_tokens: int
    completion_tokens: int
    started_at: datetime
    completed_at: datetime | None
    error_category: str | None = None


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    id: UUID
    candidate_id: UUID
    fact_requirement_id: UUID
    document_version_id: UUID
    document_chunk_id: UUID
    start_offset: int
    end_offset: int
    quote_length: int
    support_type: str
    source_authority: str
    verdict: str
    confidence: float
    reason_codes: tuple[str, ...]
    verifier_version: str
    verified_at: datetime
    document_chain_valid: bool = True


@dataclass(frozen=True, slots=True)
class SourceClaim:
    id: UUID
    claim_kind: str | None
    fact_requirement_id: UUID | None
    support_status: str
    claim_text: str


@dataclass(frozen=True, slots=True)
class SourceCitation:
    id: UUID
    answer_claim_id: UUID
    verified_evidence_id: UUID
    document_version_id: UUID
    document_chunk_id: UUID
    start_offset: int
    end_offset: int
    quote_length: int
    quote_matches_evidence: bool


@dataclass(frozen=True, slots=True)
class SourceOutbox:
    id: UUID
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    created_at: datetime
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class RunSourceSnapshot:
    tenant_id: UUID
    run_id: UUID
    conversation_id: UUID
    response_run_id: UUID
    response_status: str
    output_message_id: UUID | None
    output_message_role: str | None
    output_message_conversation_id: UUID | None
    answer_text: str | None
    mode: str
    status: str
    answer_quality: str
    stop_reason: str | None
    created_at: datetime
    hard_deadline_at: datetime
    completed_at: datetime | None
    version: int
    budget_max_llm_calls: int
    recorded_llm_call_count: int
    recorded_prompt_tokens: int
    recorded_completion_tokens: int
    facts: tuple[SourceFact, ...] = ()
    queries: tuple[SourceQuery, ...] = ()
    provider_attempts: tuple[SourceProviderAttempt, ...] = ()
    steps: tuple[SourceStep, ...] = ()
    attempts: tuple[SourceAttempt, ...] = ()
    invocations: tuple[SourceInvocation, ...] = ()
    evidence: tuple[SourceEvidence, ...] = ()
    claims: tuple[SourceClaim, ...] = ()
    citations: tuple[SourceCitation, ...] = ()
    outbox: tuple[SourceOutbox, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    source_snapshot_digest: str
    collector_schema_version: str
    source_terminal_at: datetime
    actual_mode: str
    run_status: str
    answer_quality: str
    run_stop_reason: str | None
    latency_ms: int
    minimum_required_facts: int
    fact_total: int
    fact_covered: int
    fact_gap: int
    plan_completeness_failure: bool
    factual_claim_count: int
    nonfactual_claim_count: int
    cited_factual_claim_count: int
    valid_citation_chain_count: int
    traceability_violation_count: int
    gold_assertions: tuple[GoldAssertionResult, ...]
    oracle_version: str | None
    query_pollution_count: int
    model_call_count: int
    usage: SettlementUsage
    degraded: bool
    provider_success_count: int
    provider_failure_count: int
    error_class: ErrorClass | None
    error_code: str | None
    failed_phase: str | None
    error_signal_flags: tuple[str, ...]

    @property
    def gold_assertion_total(self) -> int:
        return len(self.gold_assertions)

    @property
    def gold_assertion_passed(self) -> int:
        return sum(item.status is GoldAssertionStatus.PASS for item in self.gold_assertions)

    @property
    def gold_assertion_failed(self) -> int:
        return sum(item.status is GoldAssertionStatus.FAIL for item in self.gold_assertions)

    @property
    def gold_assertion_not_applicable(self) -> int:
        return sum(
            item.status is GoldAssertionStatus.NOT_APPLICABLE
            for item in self.gold_assertions
        )


def _not_ready(message: str, code: str) -> InvariantViolation:
    return InvariantViolation(message, code=code)


def _validate_time(value: datetime, field_name: str) -> None:
    try:
        require_aware(value, field_name)
    except ValueError as error:
        raise _not_ready("Source timestamps are not sealed", "source_timestamp_invalid") from error


def _validate_snapshot(snapshot: RunSourceSnapshot) -> None:
    if snapshot.status not in _TERMINAL_RUN_STATUSES:
        raise _not_ready("SearchRun is not terminal", "source_not_terminal")
    for field_name, value in (
        ("created_at", snapshot.created_at),
        ("hard_deadline_at", snapshot.hard_deadline_at),
    ):
        _validate_time(value, field_name)
    if snapshot.completed_at is not None:
        _validate_time(snapshot.completed_at, "completed_at")
    if snapshot.hard_deadline_at <= snapshot.created_at:
        raise _not_ready("SearchRun deadline is invalid", "source_timestamp_invalid")
    if snapshot.mode not in _MODEL_LIMIT:
        raise _not_ready("SearchRun mode is invalid", "source_topology_invalid")
    if min(
        snapshot.budget_max_llm_calls,
        snapshot.recorded_llm_call_count,
        snapshot.recorded_prompt_tokens,
        snapshot.recorded_completion_tokens,
    ) < 0:
        raise _not_ready("SearchRun usage ledger is invalid", "source_topology_invalid")

    step_by_id = {item.id: item for item in snapshot.steps}
    if len(step_by_id) != len(snapshot.steps):
        raise _not_ready("SearchRun contains duplicate Step IDs", "source_topology_invalid")
    for step in snapshot.steps:
        if step.status not in _TERMINAL_STEP_STATUSES:
            raise _not_ready("SearchRun has an unsealed Step", "source_not_sealed")
        if (step.status == StepStatus.SUCCEEDED.value) != step.output_bound:
            raise _not_ready("Step output binding is inconsistent", "source_topology_invalid")

    attempt_by_id = {item.id: item for item in snapshot.attempts}
    if len(attempt_by_id) != len(snapshot.attempts):
        raise _not_ready("SearchRun contains duplicate Attempt IDs", "source_topology_invalid")
    for attempt in snapshot.attempts:
        if attempt.step_id not in step_by_id or attempt.completed_at is None:
            raise _not_ready("SearchRun has an open or orphaned Attempt", "source_not_sealed")
        _validate_time(attempt.started_at, "attempt.started_at")
        _validate_time(attempt.completed_at, "attempt.completed_at")

    invocation_ids: set[UUID] = set()
    for invocation in snapshot.invocations:
        if invocation.id in invocation_ids:
            raise _not_ready("SearchRun contains duplicate Invocation IDs", "source_topology_invalid")
        invocation_ids.add(invocation.id)
        attempt = attempt_by_id.get(invocation.attempt_id)
        if attempt is None or attempt.step_id != invocation.step_id:
            raise _not_ready("ModelInvocation has an invalid Attempt chain", "source_topology_invalid")
        if (
            invocation.status not in _TERMINAL_INVOCATION_STATUSES
            or invocation.completed_at is None
        ):
            raise _not_ready("SearchRun has an unsealed ModelInvocation", "source_not_sealed")
        if invocation.billing_disposition not in _VALID_BILLING:
            raise _not_ready("ModelInvocation billing state is invalid", "source_topology_invalid")
        if (
            (invocation.provider_called and invocation.billing_disposition == "NOT_BILLED")
            or (
                not invocation.provider_called
                and invocation.billing_disposition != "NOT_BILLED"
            )
            or (
                invocation.status == "REUSED"
                and (
                    invocation.provider_called
                    or invocation.billing_disposition != "NOT_BILLED"
                )
            )
            or (
                invocation.billing_disposition == "BILLED"
                and invocation.status != "COMPLETED"
            )
            or (
                invocation.billing_disposition == "POSSIBLY_BILLED"
                and invocation.status not in {"FAILED", "ABANDONED"}
            )
        ):
            raise _not_ready("ModelInvocation billing binding is invalid", "source_topology_invalid")
        if min(invocation.prompt_tokens, invocation.completion_tokens) < 0:
            raise _not_ready("ModelInvocation token usage is invalid", "source_topology_invalid")
        _validate_time(invocation.started_at, "invocation.started_at")
        _validate_time(invocation.completed_at, "invocation.completed_at")

    fact_ids = {item.id for item in snapshot.facts}
    query_ids = {item.id for item in snapshot.queries}
    if len(fact_ids) != len(snapshot.facts) or len(query_ids) != len(snapshot.queries):
        raise _not_ready("SearchRun contains duplicate planning IDs", "source_topology_invalid")
    for query in snapshot.queries:
        if query.fact_requirement_id is None or query.fact_requirement_id not in fact_ids:
            raise _not_ready("QuerySpec is not bound to a Run Fact", "source_topology_invalid")
        if query.plan_revision < 1:
            raise _not_ready("QuerySpec plan revision is invalid", "source_topology_invalid")
    for provider_attempt in snapshot.provider_attempts:
        if (
            provider_attempt.query_spec_id not in query_ids
            or provider_attempt.status not in _TERMINAL_PROVIDER_STATUSES
            or provider_attempt.completed_at is None
        ):
            raise _not_ready("ProviderAttempt is open or orphaned", "source_not_sealed")
        _validate_time(provider_attempt.started_at, "provider_attempt.started_at")
        _validate_time(provider_attempt.completed_at, "provider_attempt.completed_at")

    for outbox in snapshot.outbox:
        if outbox.published_at is None:
            raise _not_ready("SearchRun has unpublished related outbox work", "source_outbox_unpublished")
        _validate_time(outbox.created_at, "outbox.created_at")
        _validate_time(outbox.published_at, "outbox.published_at")

    if snapshot.status == RunStatus.SUCCEEDED.value:
        if (
            snapshot.response_status != "SUCCEEDED"
            or snapshot.output_message_id is None
            or snapshot.output_message_role is None
            or snapshot.output_message_role.casefold() != "assistant"
            or snapshot.output_message_conversation_id != snapshot.conversation_id
            or snapshot.answer_text is None
        ):
            raise _not_ready("Successful SearchRun output is incomplete", "source_output_incomplete")

    claim_ids: set[UUID] = set()
    for claim in snapshot.claims:
        if claim.id in claim_ids:
            raise _not_ready("SearchRun contains duplicate Claim IDs", "source_topology_invalid")
        claim_ids.add(claim.id)
        if claim.claim_kind not in _VALID_CLAIM_KINDS:
            raise _not_ready("Claim kind is null or invalid", "source_claim_kind_invalid")
        if claim.claim_kind == "FACTUAL" and claim.fact_requirement_id not in fact_ids:
            raise _not_ready("Factual Claim has no valid Run Fact", "source_claim_fact_invalid")


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _valid_citation(
    citation: SourceCitation,
    claim: SourceClaim,
    evidence: SourceEvidence | None,
) -> bool:
    return bool(
        evidence is not None
        and evidence.verdict == "ACCEPTED"
        and evidence.fact_requirement_id == claim.fact_requirement_id
        and evidence.document_chain_valid
        and citation.document_version_id == evidence.document_version_id
        and citation.document_chunk_id == evidence.document_chunk_id
        and citation.start_offset == evidence.start_offset
        and citation.end_offset == evidence.end_offset
        and citation.quote_length == evidence.quote_length
        and citation.quote_matches_evidence
        and citation.start_offset >= 0
        and citation.end_offset > citation.start_offset
        and citation.end_offset - citation.start_offset == citation.quote_length
    )


def _assertion_actual(
    operator: str,
    claims: tuple[SourceClaim, ...],
    valid_evidence: tuple[SourceEvidence, ...],
) -> object:
    claim_texts = tuple(item.claim_text for item in claims)
    if operator == "set_contains":
        return claim_texts
    if operator == "source_class_at_least":
        ranks = {
            SourceAuthority.UNKNOWN.value: 0,
            SourceAuthority.INDEPENDENT.value: 1,
            SourceAuthority.OFFICIAL.value: 2,
        }
        values = [item.source_authority for item in valid_evidence]
        return max(values, key=lambda value: ranks.get(value, -1), default="UNKNOWN")
    return "\n".join(claim_texts)


def _gold_results(
    case: ShadowCase,
    claims: tuple[SourceClaim, ...],
    valid_evidence: tuple[SourceEvidence, ...],
    source_terminal_at: datetime,
) -> tuple[GoldAssertionResult, ...]:
    if case.oracle_type is not OracleType.DETERMINISTIC:
        return ()
    if (
        case.valid_from is None
        or case.valid_until is None
        or not case.valid_from <= source_terminal_at <= case.valid_until
    ):
        return tuple(
            GoldAssertionResult(
                assertion.id,
                assertion.critical,
                GoldAssertionStatus.NOT_APPLICABLE,
                "oracle_window_invalid",
            )
            for assertion in case.gold_assertions
        )
    outcomes: list[GoldAssertionResult] = []
    for assertion in case.gold_assertions:
        try:
            passed = evaluate_gold_assertion(
                assertion,
                _assertion_actual(assertion.operator, claims, valid_evidence),
            )
            reason = "assertion_passed" if passed else "actual_value_mismatch"
        except (TypeError, ValueError, ArithmeticError):
            passed = False
            reason = "actual_value_invalid"
        outcomes.append(
            GoldAssertionResult(
                assertion.id,
                assertion.critical,
                GoldAssertionStatus.PASS if passed else GoldAssertionStatus.FAIL,
                reason,
            )
        )
    return tuple(outcomes)


def _categories(snapshot: RunSourceSnapshot) -> frozenset[str]:
    values = {
        str(value)
        for value in (
            *(item.error_type for item in snapshot.provider_attempts),
            *(item.error_type for item in snapshot.attempts),
            *(item.error_category for item in snapshot.invocations),
        )
        if value is not None and str(value) in _ERROR_CATEGORY
    }
    return frozenset(values)


def _failed_phase(snapshot: RunSourceSnapshot) -> str | None:
    step_by_id = {item.id: item.step_type for item in snapshot.steps}
    phases = {
        step_by_id[item.step_id]
        for item in snapshot.attempts
        if item.error_type is not None and item.step_id in step_by_id
    }
    phases.update(
        step_by_id[item.step_id]
        for item in snapshot.invocations
        if item.error_category is not None and item.step_id in step_by_id
    )
    if any(item.status == "FAILED" for item in snapshot.provider_attempts):
        phases.add("DISCOVERY")
    return sorted(phases)[0] if phases else ("RUN" if snapshot.status != "SUCCEEDED" else None)


def _classification(
    snapshot: RunSourceSnapshot,
    candidate_signals: set[str],
    *,
    fact_gap: int,
) -> tuple[ErrorClass | None, str | None, tuple[str, ...]]:
    categories = _categories(snapshot)
    signals = set(candidate_signals)
    if "PERMANENT" in categories:
        signals.add("permanent_configuration")
    if categories & {"INTERNAL", "INFRASTRUCTURE"}:
        signals.add("infrastructure_failure")
    if "TRANSIENT" in categories:
        signals.add("provider_transient")
    if categories & {"CONTENT"} or fact_gap:
        signals.add("content_gap")
    if "MODEL_OUTPUT" in categories or "BUDGET" in categories:
        signals.add("candidate_runtime_defect")
    if snapshot.stop_reason == "INFRASTRUCTURE_FAILURE":
        signals.add("infrastructure_failure")
    elif snapshot.stop_reason == "PROVIDER_FAILURE":
        signals.add("provider_transient")
    elif snapshot.stop_reason == "INSUFFICIENT_EVIDENCE":
        signals.add("content_gap")
    elif snapshot.stop_reason == "POLICY_BLOCKED":
        signals.add("candidate_runtime_defect")
    if snapshot.status == RunStatus.CANCELLED.value:
        signals.add("unexpected_candidate_cancelled")
    if snapshot.completed_at is None or snapshot.completed_at < snapshot.created_at:
        signals.add("source_terminal_timestamp_invalid")
    if snapshot.status == RunStatus.FAILED.value and not signals:
        signals.add("unclassified_terminal_failure")

    candidate = sorted(
        item
        for item in signals
        if item
        not in {
            "permanent_configuration",
            "infrastructure_failure",
            "provider_transient",
            "content_gap",
            "unclassified_terminal_failure",
            "source_terminal_timestamp_invalid",
        }
    )
    ordered = tuple(sorted(signals))
    if candidate:
        return ErrorClass.CANDIDATE_DEFECT, candidate[0], ordered
    if "permanent_configuration" in signals:
        return ErrorClass.PERMANENT_CONFIGURATION, "permanent_configuration_error", ordered
    if (
        "infrastructure_failure" in signals
        or "unclassified_terminal_failure" in signals
        or "source_terminal_timestamp_invalid" in signals
    ):
        return ErrorClass.INFRASTRUCTURE, "infrastructure_error", ordered
    if "provider_transient" in signals:
        return ErrorClass.PROVIDER_TRANSIENT, "provider_transient_error", ordered
    if "content_gap" in signals:
        return ErrorClass.CONTENT_GAP, "content_gap", ordered
    return None, None, ordered


def _digest_payload(snapshot: RunSourceSnapshot, outcome: CollectionOutcome) -> dict[str, object]:
    """Return only metric-relevant, non-content source fields."""

    def rows(items: Iterable[object], fields: tuple[str, ...]) -> list[dict[str, object]]:
        return [
            {field: getattr(item, field) for field in fields}
            for item in sorted(items, key=lambda value: str(getattr(value, "id")))
        ]

    return {
        "schema": outcome.collector_schema_version,
        "tenant_id": snapshot.tenant_id,
        "run": {
            "id": snapshot.run_id,
            "conversation_id": snapshot.conversation_id,
            "response_run_id": snapshot.response_run_id,
            "response_status": snapshot.response_status,
            "output_message_id": snapshot.output_message_id,
            "mode": snapshot.mode,
            "status": snapshot.status,
            "answer_quality": snapshot.answer_quality,
            "stop_reason": snapshot.stop_reason,
            "created_at": snapshot.created_at,
            "hard_deadline_at": snapshot.hard_deadline_at,
            "completed_at": snapshot.completed_at,
            "version": snapshot.version,
            "budget_max_llm_calls": snapshot.budget_max_llm_calls,
            "recorded_llm_call_count": snapshot.recorded_llm_call_count,
            "recorded_prompt_tokens": snapshot.recorded_prompt_tokens,
            "recorded_completion_tokens": snapshot.recorded_completion_tokens,
        },
        "facts": rows(snapshot.facts, ("id", "required", "status", "freshness", "consequence")),
        "queries": rows(snapshot.queries, ("id", "fact_requirement_id", "plan_revision", "provider_class")),
        "provider_attempts": rows(
            snapshot.provider_attempts,
            ("id", "query_spec_id", "provider", "status", "started_at", "completed_at", "error_type"),
        ),
        "steps": rows(snapshot.steps, ("id", "step_key", "step_type", "plan_revision", "status", "output_bound")),
        "attempts": rows(snapshot.attempts, ("id", "step_id", "attempt_no", "started_at", "completed_at", "error_type")),
        "invocations": rows(
            snapshot.invocations,
            (
                "id", "step_id", "attempt_id", "role", "provider", "model", "call_no",
                "status", "billing_disposition", "provider_called", "prompt_tokens",
                "completion_tokens", "started_at", "completed_at", "error_category",
            ),
        ),
        "evidence": rows(
            snapshot.evidence,
            (
                "id", "candidate_id", "fact_requirement_id", "document_version_id",
                "document_chunk_id", "start_offset", "end_offset", "quote_length",
                "support_type", "source_authority", "verdict", "confidence",
                "reason_codes", "verifier_version", "verified_at", "document_chain_valid",
            ),
        ),
        "claims": rows(snapshot.claims, ("id", "claim_kind", "fact_requirement_id", "support_status")),
        "citations": rows(
            snapshot.citations,
            (
                "id", "answer_claim_id", "verified_evidence_id", "document_version_id",
                "document_chunk_id", "start_offset", "end_offset", "quote_length",
                "quote_matches_evidence",
            ),
        ),
        "outbox": rows(snapshot.outbox, ("id", "aggregate_type", "aggregate_id", "event_type", "created_at", "published_at")),
        "derived": {
            "query_pollution_count": outcome.query_pollution_count,
            "traceability_violation_count": outcome.traceability_violation_count,
            "gold_assertions": [
                {
                    "assertion_id": item.assertion_id,
                    "critical": item.critical,
                    "status": item.status,
                    "reason_code": item.reason_code,
                }
                for item in sorted(
                    outcome.gold_assertions,
                    key=lambda value: value.assertion_id,
                )
            ],
            "error_signal_flags": outcome.error_signal_flags,
        },
    }


def source_snapshot_digest(
    snapshot: RunSourceSnapshot,
    outcome: CollectionOutcome,
) -> str:
    """Recompute the Collector digest without re-running mutable business logic."""

    if outcome.collector_schema_version != COLLECTOR_SCHEMA_VERSION:
        raise InvariantViolation(
            "Collector schema version is not implemented by this harness",
            code="collector_schema_version_mismatch",
        )
    return hashlib.sha256(
        canonical_json_bytes(_digest_payload(snapshot, outcome))
    ).hexdigest()


def collect_run_snapshot(
    snapshot: RunSourceSnapshot,
    case: ShadowCase,
    cost_rate: CostRate,
    *,
    oracle_version: str | None,
    collector_schema_version: str = COLLECTOR_SCHEMA_VERSION,
) -> CollectionOutcome:
    """Validate one sealed snapshot and derive the immutable Result measurement."""

    _validate_snapshot(snapshot)
    if collector_schema_version != COLLECTOR_SCHEMA_VERSION:
        raise InvariantViolation(
            "Collector schema version is not implemented by this harness",
            code="collector_schema_version_mismatch",
        )
    if case.expected_mode.value not in _MODEL_LIMIT:
        raise ValueError("case expected mode is invalid")

    required_facts = tuple(item for item in snapshot.facts if item.required)
    fact_total = len(required_facts)
    fact_covered = sum(item.status in _COVERED_FACT_STATUSES for item in required_facts)
    denominator = max(fact_total, case.minimum_required_facts)
    fact_gap = denominator - fact_covered
    plan_failure = fact_total < case.minimum_required_facts

    evidence_by_id = {item.id: item for item in snapshot.evidence}
    claim_by_id = {item.id: item for item in snapshot.claims}
    valid_by_claim: dict[UUID, list[SourceEvidence]] = {}
    cited_claim_ids: set[UUID] = set()
    invalid_chain_claim_ids: set[UUID] = set()
    for citation in snapshot.citations:
        claim = claim_by_id.get(citation.answer_claim_id)
        evidence = evidence_by_id.get(citation.verified_evidence_id)
        if claim is None:
            raise _not_ready("Citation references an unknown Run Claim", "source_citation_claim_invalid")
        cited_claim_ids.add(claim.id)
        if _valid_citation(citation, claim, evidence):
            valid_by_claim.setdefault(claim.id, []).append(evidence)  # type: ignore[arg-type]
        else:
            invalid_chain_claim_ids.add(claim.id)

    factual_claims = tuple(item for item in snapshot.claims if item.claim_kind == "FACTUAL")
    nonfactual_count = len(snapshot.claims) - len(factual_claims)
    cited_factual_count = sum(item.id in cited_claim_ids for item in factual_claims)
    traceability_violations = sum(
        not valid_by_claim.get(item.id) or item.id in invalid_chain_claim_ids
        for item in factual_claims
    )
    valid_evidence = tuple(
        evidence
        for claim in factual_claims
        for evidence in valid_by_claim.get(claim.id, ())
    )

    forbidden = tuple(_normalize(item) for item in case.forbidden_query_terms)
    pollution_count = sum(
        any(term and term in _normalize(query.query_text) for term in forbidden)
        for query in snapshot.queries
    )
    terminal_valid = (
        snapshot.completed_at is not None
        and snapshot.completed_at >= snapshot.created_at
    )
    source_terminal_at = (
        snapshot.completed_at if terminal_valid else snapshot.hard_deadline_at
    )
    gold = _gold_results(
        case,
        snapshot.claims,
        valid_evidence,
        source_terminal_at,
    )

    billed = tuple(
        item
        for item in snapshot.invocations
        if item.provider_called and item.billing_disposition == "BILLED"
    )
    possibly_billed = tuple(
        item
        for item in snapshot.invocations
        if item.provider_called and item.billing_disposition == "POSSIBLY_BILLED"
    )
    provider_called = tuple(item for item in snapshot.invocations if item.provider_called)
    prompt_tokens = sum(item.prompt_tokens for item in billed)
    completion_tokens = sum(item.completion_tokens for item in billed)
    observed_cost = (
        Decimal(prompt_tokens) * cost_rate.prompt_per_million_usd
        + Decimal(completion_tokens) * cost_rate.completion_per_million_usd
    ) / Decimal(1_000_000)
    usage = SettlementUsage(
        observed_provider_calls=len(billed),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        observed_estimated_cost=observed_cost,
        possibly_billed_call_charge=len(possibly_billed),
        possibly_billed_cost_charge=(
            cost_rate.possibly_billed_run_reserve_usd if possibly_billed else Decimal(0)
        ),
    )

    candidate_signals: set[str] = set()
    if plan_failure:
        candidate_signals.add("plan_completeness_failure")
    if traceability_violations:
        candidate_signals.add("citation_traceability_violation")
    if any(
        claim_by_id[item.answer_claim_id].claim_kind != "FACTUAL"
        for item in snapshot.citations
    ):
        candidate_signals.add("nonfactual_citation")
    if pollution_count:
        candidate_signals.add("query_pollution")
    if len(provider_called) > _MODEL_LIMIT[snapshot.mode]:
        candidate_signals.add("model_call_budget_exceeded")
    if snapshot.budget_max_llm_calls != _MODEL_LIMIT[snapshot.mode]:
        candidate_signals.add("run_budget_snapshot_mismatch")
    if (
        snapshot.recorded_llm_call_count != len(provider_called)
        or snapshot.recorded_prompt_tokens != prompt_tokens
        or snapshot.recorded_completion_tokens != completion_tokens
    ):
        candidate_signals.add("model_invocation_ledger_mismatch")
    if any(item.status is GoldAssertionStatus.FAIL for item in gold):
        candidate_signals.add("gold_assertion_failure")
    if case.must_not_complete and snapshot.answer_quality == "COMPLETE":
        candidate_signals.add("unanswerable_marked_complete")
    source_class_evidence = (
        tuple(item for item in snapshot.evidence if item.document_chain_valid)
        if case.answerability is Answerability.INTENTIONALLY_UNANSWERABLE
        else valid_evidence
    )
    if (
        case.required_source_classes
        and not all(
            any(
                item.source_authority == required.value
                for item in source_class_evidence
            )
            for required in case.required_source_classes
        )
    ):
        candidate_signals.add("required_source_class_missing")
    if snapshot.mode != case.expected_mode.value:
        candidate_signals.add("mode_mismatch")

    error_class, error_code, signal_flags = _classification(
        snapshot,
        candidate_signals,
        fact_gap=fact_gap,
    )
    provider_success_count = sum(
        item.status == "SUCCEEDED" for item in snapshot.provider_attempts
    )
    provider_failure_count = len(snapshot.provider_attempts) - provider_success_count
    degraded = bool(
        provider_failure_count
        or possibly_billed
        or {"provider_transient", "infrastructure_failure"} & set(signal_flags)
    )
    deadline_latency_ms = int(
        (snapshot.hard_deadline_at - snapshot.created_at).total_seconds() * 1000
    ) + 1
    if (
        snapshot.status != RunStatus.SUCCEEDED.value
        and (
            not terminal_valid
            or (
                snapshot.completed_at is not None
                and snapshot.completed_at > snapshot.hard_deadline_at
            )
        )
    ):
        latency_ms = deadline_latency_ms
    elif snapshot.completed_at is not None and terminal_valid:
        latency_ms = int(
            (snapshot.completed_at - snapshot.created_at).total_seconds() * 1000
        )
    else:
        latency_ms = deadline_latency_ms
    preliminary = CollectionOutcome(
        source_snapshot_digest="",
        collector_schema_version=collector_schema_version,
        source_terminal_at=source_terminal_at,
        actual_mode=snapshot.mode,
        run_status=snapshot.status,
        answer_quality=snapshot.answer_quality,
        run_stop_reason=snapshot.stop_reason,
        latency_ms=latency_ms,
        minimum_required_facts=case.minimum_required_facts,
        fact_total=fact_total,
        fact_covered=fact_covered,
        fact_gap=fact_gap,
        plan_completeness_failure=plan_failure,
        factual_claim_count=len(factual_claims),
        nonfactual_claim_count=nonfactual_count,
        cited_factual_claim_count=cited_factual_count,
        valid_citation_chain_count=sum(map(len, valid_by_claim.values())),
        traceability_violation_count=traceability_violations,
        gold_assertions=gold,
        oracle_version=oracle_version if gold else None,
        query_pollution_count=pollution_count,
        model_call_count=len(provider_called),
        usage=usage,
        degraded=degraded,
        provider_success_count=provider_success_count,
        provider_failure_count=provider_failure_count,
        error_class=error_class,
        error_code=error_code,
        failed_phase=_failed_phase(snapshot),
        error_signal_flags=signal_flags,
    )
    digest = source_snapshot_digest(snapshot, preliminary)
    return replace(preliminary, source_snapshot_digest=digest)


__all__ = [
    "COLLECTOR_SCHEMA_VERSION",
    "CollectionReceipt",
    "CollectionOutcome",
    "CollectorLease",
    "GoldAssertionResult",
    "GoldAssertionStatus",
    "RunSourceSnapshot",
    "SourceAttempt",
    "SourceCitation",
    "SourceClaim",
    "SourceEvidence",
    "SourceFact",
    "SourceInvocation",
    "SourceOutbox",
    "SourceProviderAttempt",
    "SourceQuery",
    "SourceStep",
    "collect_run_snapshot",
    "source_snapshot_digest",
]
