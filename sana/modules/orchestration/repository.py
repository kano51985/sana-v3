"""Infrastructure-neutral codecs for persisted run snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sana.modules.orchestration.domain import (
    AnswerQuality,
    BudgetSnapshot,
    BudgetUsage,
    RoutingDecision,
    RunStatus,
    SearchMode,
    SearchRun,
    SearchStep,
    StepStatus,
    StepType,
    StopReason,
    ArtifactRef,
)


def budget_to_dict(budget: BudgetSnapshot) -> dict[str, Any]:
    return {
        "policy_version": budget.policy_version,
        "created_at": budget.created_at.isoformat(),
        "soft_deadline_at": budget.soft_deadline_at.isoformat(),
        "hard_deadline_at": budget.hard_deadline_at.isoformat(),
        "synthesis_reserve_seconds": budget.synthesis_reserve_seconds,
        "max_queries": budget.max_queries,
        "max_providers": budget.max_providers,
        "max_fetches": budget.max_fetches,
        "max_llm_calls": budget.max_llm_calls,
        "max_expansion_rounds": budget.max_expansion_rounds,
        "phase_seconds": dict(budget.phase_seconds),
    }


def budget_from_dict(payload: dict[str, Any]) -> BudgetSnapshot:
    return BudgetSnapshot(
        policy_version=str(payload["policy_version"]),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        soft_deadline_at=datetime.fromisoformat(str(payload["soft_deadline_at"])),
        hard_deadline_at=datetime.fromisoformat(str(payload["hard_deadline_at"])),
        synthesis_reserve_seconds=float(payload["synthesis_reserve_seconds"]),
        max_queries=int(payload["max_queries"]),
        max_providers=int(payload["max_providers"]),
        max_fetches=int(payload["max_fetches"]),
        max_llm_calls=int(payload["max_llm_calls"]),
        max_expansion_rounds=int(payload["max_expansion_rounds"]),
        phase_seconds={
            str(key): float(value)
            for key, value in dict(payload["phase_seconds"]).items()
        },
    )


def usage_to_dict(usage: BudgetUsage) -> dict[str, Any]:
    return {
        "query_count": usage.query_count,
        "provider_count": usage.provider_count,
        "fetch_count": usage.fetch_count,
        "llm_call_count": usage.llm_call_count,
        "prompt_token_count": usage.prompt_token_count,
        "completion_token_count": usage.completion_token_count,
        "expansion_rounds": usage.expansion_rounds,
        "phase_seconds": dict(usage.phase_seconds),
    }


def usage_from_dict(payload: dict[str, Any]) -> BudgetUsage:
    return BudgetUsage(
        query_count=int(payload.get("query_count", 0)),
        provider_count=int(payload.get("provider_count", 0)),
        fetch_count=int(payload.get("fetch_count", 0)),
        llm_call_count=int(payload.get("llm_call_count", 0)),
        prompt_token_count=int(payload.get("prompt_token_count", 0)),
        completion_token_count=int(payload.get("completion_token_count", 0)),
        expansion_rounds=int(payload.get("expansion_rounds", 0)),
        phase_seconds={
            str(key): float(value)
            for key, value in dict(payload.get("phase_seconds", {})).items()
        },
    )


def run_from_record(record: Any) -> SearchRun:
    routing = RoutingDecision(
        mode=SearchMode(record.mode),
        reason_codes=tuple(record.route_reason_codes),
        policy_version=record.policy_version,
        confidence=float(record.route_confidence),
    )
    return SearchRun.rehydrate(
        id=record.id,
        tenant_id=record.tenant_id,
        conversation_id=record.conversation_id,
        message_id=record.message_id,
        response_run_id=record.response_run_id,
        routing=routing,
        budget=budget_from_dict(dict(record.budget_snapshot)),
        status=RunStatus(record.status),
        answer_quality=AnswerQuality(record.answer_quality),
        stop_reason=StopReason(record.stop_reason) if record.stop_reason else None,
        usage=usage_from_dict(dict(record.usage_snapshot)),
        started_at=record.started_at,
        completed_at=record.completed_at,
        version=record.version,
    )


def artifact_to_dict(artifact: ArtifactRef) -> dict[str, str]:
    return {"uri": artifact.uri, "sha256": artifact.sha256}


def artifact_from_dict(payload: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(uri=str(payload["uri"]), sha256=str(payload["sha256"]))


def step_from_record(record: Any) -> SearchStep:
    return SearchStep.rehydrate(
        id=record.id,
        tenant_id=record.tenant_id,
        run_id=record.run_id,
        step_key=record.step_key,
        step_type=StepType(record.step_type),
        plan_revision=record.plan_revision,
        input_ref=artifact_from_dict(dict(record.input_ref)),
        status=StepStatus(record.status),
        output_ref=(
            artifact_from_dict(dict(record.output_ref))
            if record.output_ref is not None
            else None
        ),
        retry_at=record.retry_at,
        version=record.version,
    )
