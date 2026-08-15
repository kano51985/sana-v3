"""Explicitly confirmed, bounded live evaluation against the local Sana API."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import getpass
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import func, select

from sana.app.settings import SanaSettings
from sana.clients.streamlit.api_client import SanaAPIClient, SanaAPIError
from sana.platform.db.models.model_gateway import ModelInvocationRecord
from sana.platform.db.models.orchestration import SearchRunRecord
from sana.platform.db.models.search import AnswerClaim, Citation, FactRequirement
from sana.platform.db.session import create_database_engine, create_session_factory
from sana.platform.db.uow import TenantUnitOfWorkFactory


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


@dataclass(frozen=True, slots=True)
class LiveRunResult:
    case_id: str
    run_id: str
    mode: str
    status: str
    quality: str
    stop_reason: str | None
    fact_total: int
    fact_covered: int
    citation_count: int
    citation_traceability: float
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    degraded: bool
    latency_ms: int
    roles: tuple[dict[str, Any], ...]
    permanent_model_error: bool


def _load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    values = tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if not values or any(
        not isinstance(value, dict)
        or not str(value.get("id", "")).strip()
        or not str(value.get("prompt", "")).strip()
        for value in values
    ):
        raise ValueError("Live eval cases must contain non-empty id and prompt fields")
    return values


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _rates(path: Path) -> tuple[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload["version"]), dict(payload["models"])


async def _database_metrics(
    database_url: str,
    tenant_id: UUID,
    run_id: UUID,
    rates: dict[str, Any],
) -> dict[str, Any]:
    engine = create_database_engine(database_url)
    try:
        uow_factory = TenantUnitOfWorkFactory(create_session_factory(engine))
        async with uow_factory(tenant_id) as uow:
            run = await uow.session.scalar(
                select(SearchRunRecord).where(
                    SearchRunRecord.tenant_id == tenant_id,
                    SearchRunRecord.id == run_id,
                )
            )
            if run is None:
                raise RuntimeError("Live eval Run disappeared from PostgreSQL")
            fact_total = int(
                await uow.session.scalar(
                    select(func.count(FactRequirement.id)).where(
                        FactRequirement.tenant_id == tenant_id,
                        FactRequirement.run_id == run_id,
                        FactRequirement.required.is_(True),
                    )
                )
                or 0
            )
            fact_covered = int(
                await uow.session.scalar(
                    select(func.count(FactRequirement.id)).where(
                        FactRequirement.tenant_id == tenant_id,
                        FactRequirement.run_id == run_id,
                        FactRequirement.required.is_(True),
                        FactRequirement.status.in_(("COVERED", "VERIFIED")),
                    )
                )
                or 0
            )
            citation_count = int(
                await uow.session.scalar(
                    select(func.count(Citation.id)).where(
                        Citation.tenant_id == tenant_id,
                        Citation.run_id == run_id,
                    )
                )
                or 0
            )
            supported_claims = int(
                await uow.session.scalar(
                    select(func.count(AnswerClaim.id)).where(
                        AnswerClaim.tenant_id == tenant_id,
                        AnswerClaim.run_id == run_id,
                        AnswerClaim.support_status.in_(
                            ("GROUNDED", "VERIFIED", "CONFLICTED")
                        ),
                    )
                )
                or 0
            )
            cited_claims = int(
                await uow.session.scalar(
                    select(func.count(func.distinct(Citation.answer_claim_id))).where(
                        Citation.tenant_id == tenant_id,
                        Citation.run_id == run_id,
                    )
                )
                or 0
            )
            invocation_records = (
                await uow.session.scalars(
                    select(ModelInvocationRecord)
                    .where(
                        ModelInvocationRecord.tenant_id == tenant_id,
                        ModelInvocationRecord.run_id == run_id,
                    )
                    .order_by(
                        ModelInvocationRecord.started_at,
                        ModelInvocationRecord.call_no,
                    )
                )
            ).all()
            usage = dict(run.usage_snapshot or {})
            invocations = tuple(
                {
                    "role": item.role,
                    "model": item.model,
                    "provider_called": item.provider_called,
                    "status": item.status,
                    "error_category": item.error_category,
                    "prompt_tokens": item.prompt_tokens,
                    "completion_tokens": item.completion_tokens,
                }
                for item in invocation_records
            )
        role_totals: dict[tuple[str, str], dict[str, Any]] = {}
        cost = 0.0
        permanent_error = False
        for invocation in invocations:
            key = (invocation["role"], invocation["model"])
            item = role_totals.setdefault(
                key,
                {
                    "role": invocation["role"],
                    "model": invocation["model"],
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "failed": 0,
                    "reused": 0,
                },
            )
            if invocation["provider_called"]:
                item["calls"] += 1
            if invocation["status"] == "FAILED":
                item["failed"] += 1
            if invocation["status"] == "REUSED":
                item["reused"] += 1
            item["prompt_tokens"] += invocation["prompt_tokens"]
            item["completion_tokens"] += invocation["completion_tokens"]
            permanent_error = (
                permanent_error or invocation["error_category"] == "PERMANENT"
            )
            model_rate = rates.get(invocation["model"])
            if model_rate:
                cost += (
                    invocation["prompt_tokens"]
                    * float(model_rate["input_cache_miss_per_million"])
                    + invocation["completion_tokens"]
                    * float(model_rate["output_per_million"])
                ) / 1_000_000
        return {
            "fact_total": fact_total,
            "fact_covered": fact_covered,
            "citation_count": citation_count,
            "citation_traceability": (
                cited_claims / supported_claims if supported_claims else 1.0
            ),
            "model_calls": int(usage.get("llm_call_count", 0)),
            "prompt_tokens": int(usage.get("prompt_token_count", 0)),
            "completion_tokens": int(usage.get("completion_token_count", 0)),
            "estimated_cost_usd": round(cost, 8),
            "degraded": any(
                item["status"] in {"FAILED", "ABANDONED"} for item in invocations
            ),
            "roles": tuple(role_totals.values()),
            "permanent_model_error": permanent_error,
        }
    finally:
        await engine.dispose()


def _wait_for_terminal(
    client: SanaAPIClient,
    run_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = client.get_run(run_id)
        if str(run["status"]) in TERMINAL:
            return run
        time.sleep(0.5)
    raise TimeoutError("Live eval Run did not reach a terminal state")


def _run(
    *,
    client: SanaAPIClient,
    database_url: str,
    tenant_id: UUID,
    cases: tuple[dict[str, Any], ...],
    max_runs: int,
    rate_config: Path,
) -> dict[str, Any]:
    rate_version, rates = _rates(rate_config)
    conversation = client.create_conversation("Sana bounded live eval")
    results: list[LiveRunResult] = []
    for case in cases[:max_runs]:
        started = time.monotonic()
        receipt = client.submit_message(
            conversation["id"],
            str(case["prompt"]),
            idempotency_key=f"live-eval:{case['id']}:{uuid4().hex}",
        )
        run_id = str(receipt["search_run_id"])
        run = _wait_for_terminal(client, run_id, timeout_seconds=135)
        metrics = asyncio.run(
            _database_metrics(database_url, tenant_id, UUID(run_id), rates)
        )
        latency_ms = int((time.monotonic() - started) * 1_000)
        result = LiveRunResult(
            case_id=str(case["id"]),
            run_id=run_id,
            mode=str(run["mode"]),
            status=str(run["status"]),
            quality=str(run["answer_quality"]),
            stop_reason=run.get("stop_reason"),
            latency_ms=latency_ms,
            **metrics,
        )
        results.append(result)
        if result.permanent_model_error:
            break
        limit = 15_000 if result.mode == "FAST" else 120_000
        if result.latency_ms > limit or result.model_calls > (
            4 if result.mode == "FAST" else 8
        ):
            break
    latencies = [result.latency_ms for result in results]
    return {
        "sample_scope": "bounded_live_smoke_not_production_slo_proof",
        "rate_version": rate_version,
        "run_count": len(results),
        "observed_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies, default=0),
        },
        "total_model_calls": sum(item.model_calls for item in results),
        "total_prompt_tokens": sum(item.prompt_tokens for item in results),
        "total_completion_tokens": sum(item.completion_tokens for item in results),
        "estimated_cost_usd": round(
            sum(item.estimated_cost_usd for item in results), 8
        ),
        "runs": [asdict(item) for item in results],
    }


def _bounded_runs(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 20:
        raise argparse.ArgumentTypeError("max-runs must be between 1 and 20")
    return parsed


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[..., SanaAPIClient] = SanaAPIClient,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--max-runs", type=_bounded_runs, default=6)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--cases",
        type=Path,
        default=ROOT / "evals" / "live_search_cases.jsonl",
    )
    parser.add_argument(
        "--rates",
        type=Path,
        default=ROOT / "evals" / "model_rates.json",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        print("Refusing live evaluation without --confirm-live", file=sys.stderr)
        return 2

    token = os.environ.get("SANA_ACCESS_TOKEN", "").strip()
    if not token and sys.stdin.isatty():
        token = getpass.getpass("Local Sana access token: ").strip()
    if not token:
        print("SANA_ACCESS_TOKEN or interactive token input is required", file=sys.stderr)
        return 2
    client = client_factory(args.api_url, token)
    try:
        principal = client.authenticate()
        report = _run(
            client=client,
            database_url=SanaSettings().database_url,
            tenant_id=UUID(str(principal["tenant_id"])),
            cases=_load_jsonl(args.cases),
            max_runs=args.max_runs,
            rate_config=args.rates,
        )
    except (SanaAPIError, TimeoutError, RuntimeError, ValueError) as exc:
        print(f"Live eval stopped: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
