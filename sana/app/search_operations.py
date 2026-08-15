"""Concrete, bounded search operations used by the production Worker.

The operations exchange only content-addressed JSON/byte artifacts.  Database
state transitions and successor scheduling remain the completion coordinator's
responsibility so a crashed operation can be replayed without guessing which
workflow mutations committed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from sqlalchemy import select

from sana.modules.content.chunker import DocumentChunker
from sana.modules.answer.domain import ClaimKind
from sana.modules.answer.model_synthesizer import ConstrainedModelSynthesizer
from sana.modules.content.domain import (
    DocumentChunk as DomainDocumentChunk,
    DocumentVersion as DomainDocumentVersion,
    FetchArtifact,
    FetchRequest,
    FetchStatus,
)
from sana.modules.content.extractor import ContentExtractor
from sana.modules.discovery.domain import DiscoveryQuery
from sana.modules.discovery.official_sources import OfficialSourcePolicy
from sana.modules.discovery.service import DiscoveryService
from sana.modules.evidence.candidate_selector import (
    CandidateDocument,
    CandidateSelector,
)
from sana.modules.evidence.coverage import CoverageEvaluator, FactCoverage
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.evidence.model_verifier import (
    ModelEvidenceVerifier,
    evidence_from_payload,
    evidence_to_payload,
)
from sana.modules.evidence.source_authority import SourceAuthorityPolicy
from sana.modules.model_gateway.domain import ModelCallBudget, ModelInvocationContext
from sana.modules.orchestration.artifact_store import ArtifactStore
from sana.modules.orchestration.domain import ArtifactRef, SearchMode
from sana.modules.orchestration.policy import BudgetPhase
from sana.modules.orchestration.search_workflow import StepBudgetCost
from sana.modules.orchestration.step_handlers import (
    FastStepOperations,
    StepExecutionContext,
    StepExecutionResult,
)
from sana.modules.search_planning.domain import (
    Consequence,
    FactRequirement,
    FactType,
    Freshness,
    NormalizedIntent,
)
from sana.modules.search_planning.planner import SearchPlanner
from sana.modules.search_planning.query_compiler import QueryCompiler
from sana.modules.search_planning.router import AutomaticModeRouter
from sana.modules.shared.errors import ErrorCategory, TypedError
from sana.platform.db.models.conversation import Message
from sana.platform.db.uow import TenantUnitOfWorkFactory


class ContentFetcher(Protocol):
    async def fetch(self, request: FetchRequest) -> FetchArtifact: ...


@dataclass(frozen=True, slots=True)
class IntentPlanningResult:
    intent: NormalizedIntent
    llm_calls: int
    degraded: bool = False


class IntentPlanner(Protocol):
    async def plan(
        self,
        message: str,
        *,
        mode: SearchMode,
        deadline: datetime,
        max_llm_calls: int,
        invocation_context: ModelInvocationContext | None = None,
    ) -> IntentPlanningResult: ...


class ModelIntentPlanner:
    """Adapter that exposes SearchPlanner through the Worker planning port."""

    def __init__(self, planner: SearchPlanner) -> None:
        self._planner = planner
        self._fallback = HeuristicIntentPlanner(QueryCompiler().policy.version)

    async def plan(
        self,
        message: str,
        *,
        mode: SearchMode,
        deadline: datetime,
        max_llm_calls: int,
        invocation_context: ModelInvocationContext | None = None,
    ) -> IntentPlanningResult:
        budget = ModelCallBudget(
            max_calls=max(1, min(2, max_llm_calls)),
            max_total_tokens=12_000,
        )
        try:
            intent = await self._planner.plan(
                message,
                deadline=deadline,
                model_budget=budget,
                invocation_context=invocation_context,
            )
            return IntentPlanningResult(intent, budget.used_calls)
        except TypedError:
            fallback = await self._fallback.plan(
                message,
                mode=mode,
                deadline=deadline,
                max_llm_calls=max_llm_calls,
                invocation_context=invocation_context,
            )
            return IntentPlanningResult(
                fallback.intent,
                budget.used_calls,
                degraded=True,
            )


class HeuristicIntentPlanner:
    """Offline local fallback; production configuration rejects this planner."""

    _LATIN_ENTITY = re.compile(
        r"(?:[A-Z][A-Za-z0-9+.-]*)(?:\s+[A-Z][A-Za-z0-9+.-]*){0,4}"
    )
    _QUOTED_ENTITY = re.compile(r"[《\"“](.{1,48}?)[》\"”]")
    _FILLER = re.compile(
        r"(请问|请告诉我|告诉我|帮我|查一下|搜索|联网|最新|最近|当前|"
        r"是什么|有哪些|怎么样|如何|please|tell me|search|latest|current)",
        re.I,
    )
    _FACT_LABELS = {
        FactType.CHARACTER_CHANGES: ("角色改动", ("official", "patch_notes")),
        FactType.VERSION: ("当前版本", ("official", "patch_notes")),
        FactType.PATCH_NOTES: ("补丁说明", ("official", "patch_notes")),
        FactType.TEAM_META: ("当前阵容", ("guide", "official")),
        FactType.CURRENT_VALUE: ("当前数值", ("official",)),
        FactType.COMPARISON: ("方案比较", ("official", "independent")),
        FactType.BACKGROUND: ("背景资料", ("official", "independent")),
    }

    def __init__(self, policy_version: str) -> None:
        self._router = AutomaticModeRouter(policy_version)

    @classmethod
    def _entity(cls, message: str) -> str:
        quoted = cls._QUOTED_ENTITY.search(message)
        if quoted:
            return " ".join(quoted.group(1).split())
        latin = cls._LATIN_ENTITY.search(message)
        if latin:
            return " ".join(latin.group(0).split())[:64]
        cleaned = cls._FILLER.sub(" ", message)
        cleaned = re.sub(r"[!?！？。,.，:：;；\r\n]+", " ", cleaned)
        cleaned = " ".join(cleaned.split()).strip()
        if not cleaned:
            return "公开信息"
        return cleaned[:48]

    async def plan(
        self,
        message: str,
        *,
        mode: SearchMode,
        deadline: datetime,
        max_llm_calls: int,
        invocation_context: ModelInvocationContext | None = None,
    ) -> IntentPlanningResult:
        del mode, deadline, max_llm_calls, invocation_context
        fact_types = tuple(self._router.infer_fact_types(message)) or (
            FactType.BACKGROUND,
        )
        entity = self._entity(message)
        current = bool(re.search(r"(最新|最近|当前|today|latest|current)", message, re.I))
        high = bool(
            re.search(
                r"(医疗|诊断|法律|诉讼|投资|税务|medical|legal|investment|tax)",
                message,
                re.I,
            )
        )
        facts = []
        for fact_type in sorted(fact_types, key=lambda item: item.value):
            label, source_kinds = self._FACT_LABELS[fact_type]
            facts.append(
                FactRequirement(
                    key=fact_type.value,
                    fact_type=fact_type,
                    description=f"{entity}的{label}",
                    subject=entity,
                    freshness=(Freshness.CURRENT if current else Freshness.STABLE),
                    consequence=(Consequence.HIGH if high else Consequence.LOW),
                    preferred_source_kinds=source_kinds,
                )
            )
        locale = "zh-CN" if re.search(r"[\u3400-\u9fff]", message) else "en"
        return IntentPlanningResult(
            NormalizedIntent(entity, (), locale, tuple(facts)),
            0,
        )


def _reference(value: dict[str, str]) -> ArtifactRef:
    return ArtifactRef(str(value["uri"]), str(value["sha256"]))


def _reference_dict(value: ArtifactRef) -> dict[str, str]:
    return {"uri": value.uri, "sha256": value.sha256}


def _typed_error(error: TypedError | None) -> dict[str, Any] | None:
    if error is None:
        return None
    return {
        "category": error.category.value,
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }


def _fact_dict(run_id: UUID, fact: FactRequirement) -> dict[str, Any]:
    fact_id = uuid5(run_id, f"fact:{fact.key}")
    return {
        "id": str(fact_id),
        "key": fact.key,
        "fact_type": fact.fact_type.value,
        "description": fact.description,
        "subject": fact.subject,
        "required": fact.required,
        "freshness": fact.freshness.value,
        "consequence": fact.consequence.value,
        "preferred_source_kinds": list(fact.preferred_source_kinds),
    }


def _fact_from_dict(payload: dict[str, Any]) -> FactRequirement:
    return FactRequirement(
        key=str(payload["key"]),
        fact_type=FactType(str(payload["fact_type"])),
        description=str(payload["description"]),
        subject=str(payload["subject"]),
        required=bool(payload.get("required", True)),
        freshness=Freshness(str(payload.get("freshness", Freshness.STABLE.value))),
        consequence=Consequence(
            str(payload.get("consequence", Consequence.LOW.value))
        ),
        preferred_source_kinds=tuple(
            map(str, payload.get("preferred_source_kinds", ()))
        ),
    )


def _select_ranked_hits(
    candidates: tuple[dict[str, Any], ...],
    *,
    authority_policy: SourceAuthorityPolicy,
    entity: str,
    mode: SearchMode,
    max_selected_hits: int,
) -> list[dict[str, Any]]:
    classified: list[tuple[dict[str, Any], str, SourceAuthority]] = []
    for item in candidates:
        try:
            identity, authority = authority_policy.classify(
                str(item["canonical_url"]),
                entity=entity,
            )
        except ValueError:
            identity = str(item["canonical_url"])
            authority = SourceAuthority.INDEPENDENT
        classified.append((item, identity, authority))
    ranked = sorted(
        classified,
        key=lambda value: (
            0 if value[2] is SourceAuthority.OFFICIAL else 1,
            -float(value[0]["score"]),
            int(value[0]["rank"]),
            value[0]["canonical_url"],
        ),
    )
    has_official = any(
        authority is SourceAuthority.OFFICIAL
        for _, _, authority in ranked
    )
    if mode is SearchMode.FAST:
        selection_limit = 1 if has_official else min(2, max_selected_hits)
        diverse = []
        deferred = []
        seen_identities: set[str] = set()
        for item, identity, _ in ranked:
            if identity in seen_identities:
                deferred.append(item)
            else:
                diverse.append(item)
                seen_identities.add(identity)
        return (diverse + deferred)[:selection_limit]

    # Research selection is a bounded set-cover problem: cover every planned fact
    # before spending slots on redundant URLs. Once coverage is complete, prefer a
    # new publisher so cross-check requests retain source diversity.
    remaining = list(ranked)
    selected: list[dict[str, Any]] = []
    covered_fact_ids: set[str] = set()
    seen_identities: set[str] = set()
    while remaining and len(selected) < max_selected_hits:
        gains = [
            len(set(map(str, item.get("fact_ids", ()))) - covered_fact_ids)
            for item, _, _ in remaining
        ]
        maximum_gain = max(gains, default=0)
        if maximum_gain:
            candidate_indexes = [
                index for index, gain in enumerate(gains) if gain == maximum_gain
            ]
            chosen_index = min(
                candidate_indexes,
                key=lambda index: (
                    0
                    if remaining[index][2] is SourceAuthority.OFFICIAL
                    else 1,
                    0 if remaining[index][1] not in seen_identities else 1,
                    index,
                ),
            )
        else:
            chosen_index = min(
                range(len(remaining)),
                key=lambda index: (
                    0 if remaining[index][1] not in seen_identities else 1,
                    0
                    if remaining[index][2] is SourceAuthority.OFFICIAL
                    else 1,
                    index,
                ),
            )
        item, identity, _ = remaining.pop(chosen_index)
        selected.append(item)
        seen_identities.add(identity)
        covered_fact_ids.update(map(str, item.get("fact_ids", ())))
    return selected


def _model_context(context: StepExecutionContext) -> ModelInvocationContext:
    return ModelInvocationContext(
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        step_id=context.step_id,
        step_key=context.step_key,
        attempt_id=context.attempt_id,
        attempt_no=context.attempt_no,
        trace_context=context.trace_context,
        input_refs=(f"artifact-sha256:{context.input_ref.sha256}",),
    )


@dataclass(slots=True)
class SearchStepOperations:
    uow_factory: TenantUnitOfWorkFactory
    artifacts: ArtifactStore
    planner: IntentPlanner
    discovery: DiscoveryService
    fetcher: ContentFetcher
    provider_names: tuple[str, ...]
    max_selected_hits: int = 4
    model_verifier: ModelEvidenceVerifier | None = None
    model_synthesizer: ConstrainedModelSynthesizer | None = None
    _compiler: QueryCompiler = field(init=False, repr=False)
    _extractor: ContentExtractor = field(init=False, repr=False)
    _chunker: DocumentChunker = field(init=False, repr=False)
    _candidate_selector: CandidateSelector = field(init=False, repr=False)
    _authority: SourceAuthorityPolicy = field(init=False, repr=False)
    _official_sources: OfficialSourcePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.provider_names:
            raise ValueError("At least one discovery provider is required")
        if self.max_selected_hits < 1:
            raise ValueError("max_selected_hits must be positive")
        self._compiler = QueryCompiler()
        self._extractor = ContentExtractor()
        self._chunker = DocumentChunker()
        self._authority = SourceAuthorityPolicy()
        self._candidate_selector = CandidateSelector(self._authority)
        self._official_sources = OfficialSourcePolicy()

    def registry_operations(self) -> FastStepOperations:
        return FastStepOperations(
            self.route,
            self.plan,
            self.discovery_step,
            self.select,
            self.fetch,
            self.extract,
            self.verify,
            self.synthesize,
        )

    async def _json(self, context: StepExecutionContext) -> dict[str, Any]:
        payload = await self.artifacts.get_json(context.tenant_id, context.input_ref)
        if not isinstance(payload, dict):
            raise TypedError(
                ErrorCategory.CONTENT,
                "step_input_invalid",
                "Step input artifact must be a JSON object",
                retryable=False,
            )
        return payload

    async def _result(
        self,
        context: StepExecutionContext,
        payload: dict[str, Any],
        cost: StepBudgetCost,
    ) -> StepExecutionResult:
        reference = await self.artifacts.put_json(
            context.tenant_id,
            context.run_id,
            payload,
        )
        return StepExecutionResult(reference, cost)

    async def route(self, context: StepExecutionContext) -> StepExecutionResult:
        prefix = "db://messages/"
        if not context.input_ref.uri.startswith(prefix):
            raise TypedError(
                ErrorCategory.CONTENT,
                "route_input_invalid",
                "Route input must reference a persisted message",
                retryable=False,
            )
        try:
            message_id = UUID(context.input_ref.uri[len(prefix) :])
        except ValueError as exc:
            raise TypedError(
                ErrorCategory.CONTENT,
                "route_message_id_invalid",
                "Route message reference is invalid",
                retryable=False,
                cause=exc,
            ) from exc
        async with self.uow_factory(context.tenant_id) as uow:
            message = (
                await uow.session.execute(
                    select(Message.id, Message.content).where(
                        Message.tenant_id == context.tenant_id,
                        Message.id == message_id,
                    )
                )
            ).one_or_none()
        if message is None:
            raise TypedError(
                ErrorCategory.PERMANENT,
                "message_not_found",
                "The submitted message no longer exists",
                retryable=False,
            )
        persisted_message_id, content = message
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest != context.input_ref.sha256:
            raise TypedError(
                ErrorCategory.CONTENT,
                "message_digest_mismatch",
                "The submitted message changed after Run creation",
                retryable=False,
            )
        return await self._result(
            context,
            {
                "schema": "sana.route.v1",
                "message_id": str(persisted_message_id),
                "message": content,
            },
            StepBudgetCost(BudgetPhase.ROUTE_PLAN),
        )

    async def plan(self, context: StepExecutionContext) -> StepExecutionResult:
        route = await self._json(context)
        async with self.uow_factory(context.tenant_id) as uow:
            run = await uow.runs.get(context.tenant_id, context.run_id)
        if run is None:
            raise TypedError(
                ErrorCategory.PERMANENT,
                "run_not_found",
                "Search Run no longer exists",
                retryable=False,
            )
        planning = await self.planner.plan(
            str(route["message"]),
            mode=run.mode,
            deadline=context.deadline_at,
            max_llm_calls=run.budget.max_llm_calls,
            invocation_context=_model_context(context),
        )
        intent = planning.intent
        queries = self._compiler.compile(intent, run.mode)
        fact_payloads = [_fact_dict(context.run_id, fact) for fact in intent.facts]
        fact_ids = {item["key"]: item["id"] for item in fact_payloads}
        query_payloads = [
            {
                "id": str(uuid5(context.run_id, f"query:{query.key}")),
                "key": query.key,
                "fact_key": query.fact_key,
                "fact_id": fact_ids[query.fact_key],
                "text": query.text,
                "signature": query.signature,
                "locale": query.locale,
                "freshness_days": query.freshness_days,
                "plan_revision": query.plan_revision,
                "metadata": dict(query.metadata),
                "direct_urls": list(
                    self._official_sources.urls_for(
                        intent.entity,
                        next(
                            fact.fact_type
                            for fact in intent.facts
                            if fact.key == query.fact_key
                        ),
                    )
                ),
            }
            for query in queries
        ]
        return await self._result(
            context,
            {
                "schema": "sana.plan.v1",
                "message": str(route["message"]),
                "mode": run.mode.value,
                "degraded": planning.degraded,
                "intent": {
                    "entity": intent.entity,
                    "aliases": list(intent.aliases),
                    "locale": intent.locale,
                    "requires_comparison": intent.requires_comparison,
                    "requires_complete_sources": intent.requires_complete_sources,
                },
                "facts": fact_payloads,
                "queries": query_payloads,
                "providers": list(self.provider_names),
            },
            StepBudgetCost(
                BudgetPhase.ROUTE_PLAN,
                llm_calls=planning.llm_calls,
            ),
        )

    async def discovery_step(
        self,
        context: StepExecutionContext,
    ) -> StepExecutionResult:
        payload = await self._json(context)
        query = dict(payload["query"])
        plan = await self.artifacts.get_json(
            context.tenant_id,
            _reference(dict(payload["plan_ref"])),
        )
        if not isinstance(plan, dict):
            raise TypedError(
                ErrorCategory.CONTENT,
                "discovery_plan_invalid",
                "Discovery plan artifact must contain a JSON object",
                retryable=False,
            )
        mode = SearchMode(str(plan["mode"]))
        provider_window = 2.0 if mode is SearchMode.FAST else 8.0
        provider_deadline = min(
            context.deadline_at,
            context.clock.now() + timedelta(seconds=provider_window),
        )
        provider_names = tuple(map(str, payload.get("providers", self.provider_names)))
        responses = await self.discovery.discover(
            context.tenant_id,
            (
                DiscoveryQuery(
                    key=str(query["key"]),
                    text=str(query["text"]),
                    locale=str(query["locale"]),
                    freshness_days=query.get("freshness_days"),
                    direct_urls=tuple(map(str, query.get("direct_urls", ()))),
                ),
            ),
            provider_names,
            deadline=provider_deadline,
        )
        serialized = []
        for response in responses:
            serialized.append(
                {
                    "provider": response.provider,
                    "query_key": response.query_key,
                    "metrics": {
                        "elapsed_ms": response.metrics.elapsed_ms,
                        "response_bytes": response.metrics.response_bytes,
                        "raw_hit_count": response.metrics.raw_hit_count,
                    },
                    "error": _typed_error(response.error),
                    "hits": [
                        {
                            "id": str(
                                uuid5(
                                    context.run_id,
                                    f"hit:{response.provider}:{response.query_key}:"
                                    f"{hit.canonical_url}",
                                )
                            ),
                            "provider": hit.provider,
                            "query_key": hit.query_key,
                            "query_id": query["id"],
                            "fact_id": query["fact_id"],
                            "rank": hit.rank,
                            "url": hit.url,
                            "canonical_url": hit.canonical_url,
                            "title": hit.title,
                            "snippet": hit.snippet,
                            "score": hit.score,
                            "published_at": (
                                hit.published_at.isoformat()
                                if hit.published_at is not None
                                else None
                            ),
                        }
                        for hit in response.hits
                    ],
                }
            )
        return await self._result(
            context,
            {
                "schema": "sana.discovery.v1",
                "plan_ref": payload["plan_ref"],
                "query": query,
                "responses": serialized,
            },
            StepBudgetCost(
                BudgetPhase.DISCOVERY,
                queries=1,
                providers=sum(name != "direct" for name in provider_names),
            ),
        )

    async def select(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        plan = await self.artifacts.get_json(
            context.tenant_id,
            _reference(dict(payload["plan_ref"])),
        )
        if not isinstance(plan, dict):
            raise TypedError(
                ErrorCategory.CONTENT,
                "selection_plan_invalid",
                "Selection plan artifact must contain a JSON object",
                retryable=False,
            )
        mode = SearchMode(str(plan["mode"]))
        entity = str(plan["intent"]["entity"])
        candidates: dict[str, dict[str, Any]] = {}
        provider_failures = 0
        for raw_ref in payload.get("discovery_refs", []):
            discovery = await self.artifacts.get_json(
                context.tenant_id,
                _reference(dict(raw_ref)),
            )
            for response in discovery.get("responses", []):
                if response.get("error") is not None:
                    provider_failures += 1
                for hit in response.get("hits", []):
                    canonical = str(hit["canonical_url"])
                    previous = candidates.get(canonical)
                    fact_id = str(hit["fact_id"])
                    if previous is None:
                        merged = dict(hit)
                        merged["fact_ids"] = [fact_id]
                        candidates[canonical] = merged
                        continue
                    fact_ids = list(map(str, previous.get("fact_ids", ())))
                    if fact_id not in fact_ids:
                        fact_ids.append(fact_id)
                    if float(hit["score"]) > float(previous["score"]):
                        merged = dict(hit)
                        merged["fact_ids"] = fact_ids
                        candidates[canonical] = merged
                    else:
                        previous["fact_ids"] = fact_ids
        selected = _select_ranked_hits(
            tuple(candidates.values()),
            authority_policy=self._authority,
            entity=entity,
            mode=mode,
            max_selected_hits=self.max_selected_hits,
        )
        return await self._result(
            context,
            {
                "schema": "sana.selection.v1",
                "plan_ref": payload["plan_ref"],
                "selected": selected,
                "provider_failures": provider_failures,
            },
            StepBudgetCost(BudgetPhase.DISCOVERY),
        )

    async def fetch(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        hit = dict(payload["hit"])
        fetched = await self.fetcher.fetch(
            FetchRequest(str(hit["canonical_url"]), context.deadline_at)
        )
        if fetched.status is not FetchStatus.SUCCEEDED:
            assert fetched.error is not None
            raise fetched.error
        body_ref = await self.artifacts.put_bytes(
            context.tenant_id,
            context.run_id,
            fetched.body,
        )
        return await self._result(
            context,
            {
                "schema": "sana.fetch.v1",
                "plan_ref": payload["plan_ref"],
                "hit": hit,
                "request_url": fetched.request_url,
                "final_url": fetched.final_url,
                "http_status": fetched.http_status,
                "media_type": fetched.media_type,
                "content_hash": fetched.content_hash,
                "fetched_at": fetched.fetched_at.isoformat(),
                "redirects": list(fetched.redirects),
                "response_headers": dict(fetched.response_headers),
                "body_ref": _reference_dict(body_ref),
            },
            StepBudgetCost(BudgetPhase.FETCH_EXTRACT, fetches=1),
        )

    async def extract(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        body = await self.artifacts.get_bytes(
            context.tenant_id,
            _reference(dict(payload["body_ref"])),
        )
        artifact = FetchArtifact(
            request_url=str(payload["request_url"]),
            final_url=str(payload["final_url"]),
            status=FetchStatus.SUCCEEDED,
            http_status=int(payload["http_status"]),
            media_type=str(payload["media_type"]),
            body=body,
            content_hash=str(payload["content_hash"]),
            fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
            redirects=tuple(map(str, payload.get("redirects", []))),
            response_headers=dict(payload.get("response_headers", {})),
        )
        extracted = self._extractor.extract(artifact)
        text_ref = await self.artifacts.put_bytes(
            context.tenant_id,
            context.run_id,
            extracted.text.encode("utf-8"),
        )
        canonical_hash = hashlib.sha256(
            extracted.canonical_url.encode("utf-8")
        ).hexdigest()
        document_id = uuid5(context.tenant_id, f"document:{canonical_hash}")
        version_id = uuid5(
            document_id,
            "version:"
            f"{hashlib.sha256(extracted.text.encode('utf-8')).hexdigest()}",
        )
        chunks = self._chunker.chunk(extracted.text)
        return await self._result(
            context,
            {
                "schema": "sana.extract.v1",
                "plan_ref": payload["plan_ref"],
                "hit": payload["hit"],
                "fetch": {
                    "request_url": payload["request_url"],
                    "final_url": payload["final_url"],
                    "http_status": payload["http_status"],
                    "media_type": payload["media_type"],
                    "content_hash": payload["content_hash"],
                    "fetched_at": payload["fetched_at"],
                    "body_ref": payload["body_ref"],
                },
                "document": {
                    "id": str(document_id),
                    "canonical_url": extracted.canonical_url,
                    "canonical_url_hash": canonical_hash,
                    "title": extracted.title,
                    "source_host": urlsplit(extracted.canonical_url).hostname or "",
                },
                "version": {
                    "id": str(version_id),
                    "content_hash": hashlib.sha256(extracted.text.encode("utf-8")).hexdigest(),
                    "text_ref": _reference_dict(text_ref),
                    "media_type": extracted.media_type,
                    "language": extracted.language,
                    "text_length": len(extracted.text),
                    "fetched_at": extracted.fetched_at.isoformat(),
                },
                "chunks": [
                    {
                        "id": str(uuid5(version_id, f"chunk:{chunk.ordinal}")),
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                        "token_count": chunk.token_count,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                    }
                    for chunk in chunks
                ],
            },
            StepBudgetCost(BudgetPhase.FETCH_EXTRACT),
        )

    async def verify(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        plan = await self.artifacts.get_json(
            context.tenant_id,
            _reference(dict(payload["plan_ref"])),
        )
        facts = {
            UUID(str(raw["id"])): _fact_from_dict(dict(raw))
            for raw in plan.get("facts", ())
        }
        documents: list[CandidateDocument] = []
        for raw_ref in payload.get("extract_refs", ()):
            extracted = await self.artifacts.get_json(
                context.tenant_id,
                _reference(dict(raw_ref)),
            )
            if not isinstance(extracted, dict):
                continue
            text = (
                await self.artifacts.get_bytes(
                    context.tenant_id,
                    _reference(dict(extracted["version"]["text_ref"])),
                )
            ).decode("utf-8")
            document_id = UUID(str(extracted["document"]["id"]))
            version = DomainDocumentVersion(
                UUID(str(extracted["version"]["id"])),
                context.tenant_id,
                document_id,
                str(extracted["version"]["content_hash"]),
                text,
                str(extracted["version"]["media_type"]),
                extracted["version"].get("language"),
                datetime.fromisoformat(str(extracted["version"]["fetched_at"])),
            )
            chunks = tuple(
                (
                    UUID(str(raw_chunk["id"])),
                    DomainDocumentChunk(
                        int(raw_chunk["ordinal"]),
                        str(raw_chunk["text"]),
                        str(raw_chunk["text_hash"]),
                        int(raw_chunk["token_count"]),
                        int(raw_chunk["start_offset"]),
                        int(raw_chunk["end_offset"]),
                    ),
                )
                for raw_chunk in extracted.get("chunks", ())
            )
            documents.append(
                CandidateDocument(
                    document_id,
                    version,
                    chunks,
                    str(extracted["document"]["canonical_url"]),
                    str(extracted["document"]["title"]),
                    tuple(
                        UUID(str(value))
                        for value in (
                            extracted["hit"].get("fact_ids")
                            or (extracted["hit"]["fact_id"],)
                        )
                    ),
                )
            )
        candidates = self._candidate_selector.select(
            run_id=context.run_id,
            entity=str(plan["intent"]["entity"]),
            facts=facts,
            documents=tuple(documents),
        )
        if self.model_verifier is None:
            batch = ModelEvidenceVerifier.deterministic(
                candidates,
                run_id=context.run_id,
                verified_at=context.clock.now(),
            )
        else:
            batch = await self.model_verifier.verify(
                candidates,
                invocation_context=_model_context(context),
                deadline=context.deadline_at,
                verified_at=context.clock.now(),
            )
        coverage = {
            fact_id: CoverageEvaluator().evaluate(
                context.tenant_id,
                context.run_id,
                fact_id,
                fact,
                batch.evidence,
            )
            for fact_id, fact in facts.items()
        }
        return await self._result(
            context,
            {
                "schema": "sana.verify.v2",
                "plan_ref": payload["plan_ref"],
                "evidence": [evidence_to_payload(item) for item in batch.evidence],
                "coverage": [
                    {
                        "fact_id": str(fact_id),
                        "fact_key": assessment.fact_key,
                        "status": assessment.status.value,
                        "level": (
                            assessment.level.value
                            if assessment.level is not None
                            else None
                        ),
                        "evidence_ids": list(map(str, assessment.evidence_ids)),
                        "supporting_ids": list(map(str, assessment.supporting_ids)),
                        "contradicting_ids": list(
                            map(str, assessment.contradicting_ids)
                        ),
                        "reason_codes": list(assessment.reason_codes),
                    }
                    for fact_id, assessment in coverage.items()
                ],
                "degraded": batch.degraded,
                "degradation_code": batch.degradation_code,
            },
            StepBudgetCost(BudgetPhase.VERIFY),
        )

    async def synthesize(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        plan = await self.artifacts.get_json(
            context.tenant_id,
            _reference(dict(payload["plan_ref"])),
        )
        verification: dict[str, Any] = {}
        raw_verify_ref = payload.get("verify_ref")
        if raw_verify_ref is not None:
            loaded = await self.artifacts.get_json(
                context.tenant_id,
                _reference(dict(raw_verify_ref)),
            )
            if isinstance(loaded, dict):
                verification = loaded
        facts = {
            UUID(str(raw["id"])): _fact_from_dict(dict(raw))
            for raw in plan.get("facts", ())
        }
        evidence = tuple(
            evidence_from_payload(
                dict(raw),
                tenant_id=context.tenant_id,
                run_id=context.run_id,
            )
            for raw in verification.get("evidence", ())
        )
        coverage = {
            fact_id: CoverageEvaluator().evaluate(
                context.tenant_id,
                context.run_id,
                fact_id,
                fact,
                evidence,
            )
            for fact_id, fact in facts.items()
        }
        pipeline_degradation_codes = tuple(
            str(value)
            for value in payload.get("pipeline_degradation_codes", ())
            if value
        )
        if self.model_synthesizer is None:
            synthesized = ConstrainedModelSynthesizer.deterministic(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                facts=facts,
                coverage=coverage,
                evidence=evidence,
            )
        else:
            synthesized = await self.model_synthesizer.synthesize(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                facts=facts,
                coverage=coverage,
                evidence=evidence,
                invocation_context=_model_context(context),
                deadline=context.deadline_at,
            )
        model_pipeline_degraded = (
            bool(plan.get("degraded"))
            or bool(verification.get("degraded"))
            or synthesized.degraded
        )
        degraded = model_pipeline_degraded or bool(pipeline_degradation_codes)
        required_ids = {
            fact_id for fact_id, fact in facts.items() if fact.required
        }
        supported_fact_ids = {
            claim.fact_requirement_id
            for claim in synthesized.answer.claims
            if claim.kind is ClaimKind.FACTUAL and claim.evidence_ids
        }
        complete = bool(required_ids) and required_ids <= supported_fact_ids and all(
            coverage[fact_id].status in {FactCoverage.COVERED, FactCoverage.VERIFIED}
            for fact_id in required_ids
        ) and not degraded
        zh = str(plan["intent"]["locale"]).lower().startswith("zh")
        lines = [
            "我找到并核对了以下网页原文：" if zh else "I found and checked these source excerpts:"
        ]
        citations_by_claim: dict[UUID, list[Any]] = {}
        for citation in synthesized.answer.citations:
            citations_by_claim.setdefault(citation.answer_claim_id, []).append(citation)
        claims = []
        for claim in synthesized.answer.claims:
            claim_citations = citations_by_claim.get(claim.id, [])
            links = " ".join(
                f"[{citation.label}]({citation.rendered_url})"
                for citation in claim_citations
            )
            lines.append(f"- {claim.text}{(' ' + links) if links else ''}")
            claims.append(
                {
                    "id": str(claim.id),
                    "claim_key": claim.claim_key,
                    "text": claim.text,
                    "kind": claim.kind.value,
                    "support_status": claim.support.value,
                    "fact_id": (
                        str(claim.fact_requirement_id)
                        if claim.fact_requirement_id is not None
                        else None
                    ),
                    "evidence_ids": list(map(str, claim.evidence_ids)),
                    "citations": [
                        {
                            "id": str(citation.id),
                            "evidence_id": str(citation.verified_evidence_id),
                            "ordinal": citation.ordinal,
                            "label": citation.label,
                            "url": citation.rendered_url,
                            "document_version_id": str(
                                citation.document_version_id
                            ),
                            "document_chunk_id": str(citation.document_chunk_id),
                            "quote": citation.quote,
                            "start_offset": citation.start_offset,
                            "end_offset": citation.end_offset,
                        }
                        for citation in claim_citations
                    ],
                }
            )
        missing_ids = required_ids - supported_fact_ids
        missing = [facts[fact_id].description for fact_id in missing_ids]
        if missing:
            prefix = "仍未获得足够证据" if zh else "Evidence is still insufficient"
            lines.append(f"\n{prefix}：" + "、".join(missing))
        if len(lines) == 1:
            lines.append(
                "- 暂无可验证的网页证据。" if zh else "- No verifiable web evidence is available."
            )
        if complete:
            quality = "COMPLETE"
            reason = "FACTS_COVERED"
        else:
            quality = "PARTIAL"
            reason = (
                "TIME_BUDGET"
                if context.clock.now() >= context.deadline_at
                or "phase_deadline_exceeded" in pipeline_degradation_codes
                else "PROVIDER_FAILURE"
                if model_pipeline_degraded
                or (not claims and int(payload.get("provider_failures", 0)) > 0)
                else "INSUFFICIENT_EVIDENCE"
            )
        return await self._result(
            context,
            {
                "schema": "sana.answer.v1",
                "answer": "\n".join(lines),
                "quality": quality,
                "stop_reason": reason,
                "claims": claims,
                "missing_fact_ids": list(map(str, missing_ids)),
                "degraded": degraded,
                "degradation_codes": list(
                    dict.fromkeys(
                        value
                        for value in (
                            *pipeline_degradation_codes,
                            verification.get("degradation_code"),
                            synthesized.degradation_code,
                        )
                        if value
                    )
                ),
            },
            StepBudgetCost(BudgetPhase.SYNTHESIZE),
        )


__all__ = [
    "HeuristicIntentPlanner",
    "IntentPlanner",
    "IntentPlanningResult",
    "ModelIntentPlanner",
    "SearchStepOperations",
]
