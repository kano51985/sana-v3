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
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from sqlalchemy import select

from sana.modules.content.chunker import DocumentChunker
from sana.modules.content.domain import FetchArtifact, FetchRequest, FetchStatus
from sana.modules.content.extractor import ContentExtractor
from sana.modules.discovery.domain import DiscoveryQuery
from sana.modules.discovery.service import DiscoveryService
from sana.modules.model_gateway.domain import ModelCallBudget
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


class IntentPlanner(Protocol):
    async def plan(
        self,
        message: str,
        *,
        mode: SearchMode,
        deadline: datetime,
        max_llm_calls: int,
    ) -> tuple[NormalizedIntent, int]: ...


class ModelIntentPlanner:
    """Adapter that exposes SearchPlanner through the Worker planning port."""

    def __init__(self, planner: SearchPlanner) -> None:
        self._planner = planner

    async def plan(
        self,
        message: str,
        *,
        mode: SearchMode,
        deadline: datetime,
        max_llm_calls: int,
    ) -> tuple[NormalizedIntent, int]:
        del mode
        budget = ModelCallBudget(
            max_calls=max(1, min(2, max_llm_calls)),
            max_total_tokens=12_000,
        )
        intent = await self._planner.plan(
            message,
            deadline=deadline,
            model_budget=budget,
        )
        return intent, budget.used_calls


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
    ) -> tuple[NormalizedIntent, int]:
        del mode, deadline, max_llm_calls
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
        return NormalizedIntent(entity, (), locale, tuple(facts)), 0


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


@dataclass(slots=True)
class SearchStepOperations:
    uow_factory: TenantUnitOfWorkFactory
    artifacts: ArtifactStore
    planner: IntentPlanner
    discovery: DiscoveryService
    fetcher: ContentFetcher
    provider_names: tuple[str, ...]
    max_selected_hits: int = 4
    _compiler: QueryCompiler = field(init=False, repr=False)
    _extractor: ContentExtractor = field(init=False, repr=False)
    _chunker: DocumentChunker = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.provider_names:
            raise ValueError("At least one discovery provider is required")
        if self.max_selected_hits < 1:
            raise ValueError("max_selected_hits must be positive")
        self._compiler = QueryCompiler()
        self._extractor = ContentExtractor()
        self._chunker = DocumentChunker()

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
        intent, llm_calls = await self.planner.plan(
            str(route["message"]),
            mode=run.mode,
            deadline=context.deadline_at,
            max_llm_calls=run.budget.max_llm_calls,
        )
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
            }
            for query in queries
        ]
        return await self._result(
            context,
            {
                "schema": "sana.plan.v1",
                "message": str(route["message"]),
                "mode": run.mode.value,
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
            StepBudgetCost(BudgetPhase.ROUTE_PLAN, llm_calls=llm_calls),
        )

    async def discovery_step(
        self,
        context: StepExecutionContext,
    ) -> StepExecutionResult:
        payload = await self._json(context)
        query = dict(payload["query"])
        provider_names = tuple(map(str, payload.get("providers", self.provider_names)))
        responses = await self.discovery.discover(
            context.tenant_id,
            (
                DiscoveryQuery(
                    key=str(query["key"]),
                    text=str(query["text"]),
                    locale=str(query["locale"]),
                    freshness_days=query.get("freshness_days"),
                ),
            ),
            provider_names,
            deadline=context.deadline_at,
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
                providers=len(provider_names),
            ),
        )

    async def select(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
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
                    if previous is None or float(hit["score"]) > float(previous["score"]):
                        candidates[canonical] = dict(hit)
        selected = sorted(
            candidates.values(),
            key=lambda item: (-float(item["score"]), int(item["rank"]), item["canonical_url"]),
        )[: self.max_selected_hits]
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
        version_id = uuid5(document_id, f"version:{hashlib.sha256(extracted.text.encode('utf-8')).hexdigest()}")
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

    @staticmethod
    def _fact_terms(plan: dict[str, Any], fact: dict[str, Any]) -> tuple[str, ...]:
        mapping = {
            FactType.CHARACTER_CHANGES.value: ("改动", "调整", "buff", "nerf", "change"),
            FactType.VERSION.value: ("版本", "赛季", "version", "season"),
            FactType.PATCH_NOTES.value: ("补丁", "更新", "patch", "changelog"),
            FactType.TEAM_META.value: ("阵容", "配队", "meta", "lineup", "team"),
            FactType.CURRENT_VALUE.value: ("当前", "价格", "current", "price", "score"),
            FactType.COMPARISON.value: ("比较", "对比", "compare", "versus"),
            FactType.BACKGROUND.value: (),
        }
        entity = str(plan["intent"]["entity"]).casefold()
        subject = str(fact["subject"]).casefold()
        return tuple(dict.fromkeys((entity, subject, *mapping[str(fact["fact_type"])])))

    async def verify(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        plan = await self.artifacts.get_json(
            context.tenant_id,
            _reference(dict(payload["plan_ref"])),
        )
        hit = dict(payload["hit"])
        fact = next(
            (item for item in plan["facts"] if item["id"] == hit["fact_id"]),
            None,
        )
        if fact is None:
            raise TypedError(
                ErrorCategory.CONTENT,
                "verification_fact_missing",
                "Selected hit does not map to a planned fact",
                retryable=False,
            )
        terms = self._fact_terms(plan, fact)
        entity_terms = {str(plan["intent"]["entity"]).casefold(), str(fact["subject"]).casefold()}
        keyword_terms = set(terms) - entity_terms
        ranked = []
        for chunk in payload.get("chunks", []):
            text = str(chunk["text"])
            folded = text.casefold()
            matched = {term for term in terms if term and term in folded}
            ranked.append((len(matched), matched, chunk))
        ranked.sort(key=lambda item: (-item[0], int(item[2]["ordinal"])))
        accepted = False
        evidence = None
        if ranked and ranked[0][0] > 0:
            _, matched, chunk = ranked[0]
            entity_match = bool(matched & entity_terms)
            keyword_match = bool(matched & keyword_terms) or not keyword_terms
            accepted = entity_match and keyword_match
            quote = str(chunk["text"])[:600]
            quote_start = int(chunk["start_offset"])
            candidate_id = uuid5(
                context.run_id,
                f"evidence:{fact['id']}:{chunk['id']}:{hashlib.sha256(quote.encode('utf-8')).hexdigest()}",
            )
            verified_id = uuid5(candidate_id, "verified:lexical-v1")
            evidence = {
                "candidate_id": str(candidate_id),
                "verified_id": str(verified_id),
                "fact_id": fact["id"],
                "document_id": payload["document"]["id"],
                "document_version_id": payload["version"]["id"],
                "document_chunk_id": chunk["id"],
                "quote": quote,
                "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                "start_offset": quote_start,
                "end_offset": quote_start + len(quote),
                "support_type": "SUPPORTS",
                "candidate_score": min(1.0, len(matched) / max(1, len(terms))),
                "verdict": "ACCEPTED" if accepted else "REJECTED",
                "confidence": 0.65 if accepted else 0.2,
                "reason_codes": [
                    "exact_source_span",
                    "lexical_fact_match" if accepted else "insufficient_lexical_match",
                ],
                "verifier_version": "deterministic-lexical-v1",
                "verified_at": context.clock.now().isoformat(),
                "url": payload["document"]["canonical_url"],
                "title": payload["document"]["title"],
            }
        return await self._result(
            context,
            {
                "schema": "sana.verify.v1",
                "plan_ref": payload["plan_ref"],
                "fact": fact,
                "accepted": accepted,
                "evidence": evidence,
            },
            StepBudgetCost(BudgetPhase.VERIFY),
        )

    async def synthesize(self, context: StepExecutionContext) -> StepExecutionResult:
        payload = await self._json(context)
        plan = await self.artifacts.get_json(
            context.tenant_id,
            _reference(dict(payload["plan_ref"])),
        )
        accepted_by_fact: dict[str, list[dict[str, Any]]] = {}
        for raw_ref in payload.get("verify_refs", []):
            verification = await self.artifacts.get_json(
                context.tenant_id,
                _reference(dict(raw_ref)),
            )
            if verification.get("accepted") and verification.get("evidence"):
                accepted_by_fact.setdefault(
                    str(verification["fact"]["id"]), []
                ).append(dict(verification["evidence"]))
        required = [fact for fact in plan["facts"] if fact.get("required", True)]
        complete = bool(required) and all(fact["id"] in accepted_by_fact for fact in required)
        zh = str(plan["intent"]["locale"]).lower().startswith("zh")
        lines = [
            "我找到并核对了以下网页原文：" if zh else "I found and checked these source excerpts:"
        ]
        claims = []
        missing = []
        for fact in required:
            evidence_items = accepted_by_fact.get(str(fact["id"]), [])
            if not evidence_items:
                missing.append(str(fact["description"]))
                continue
            evidence = evidence_items[0]
            quote = str(evidence["quote"]).replace("\n", " ").strip()
            if len(quote) > 280:
                quote = quote[:277].rstrip() + "…"
            lines.append(
                f"- **{fact['description']}**：{quote} ([来源]({evidence['url']}))"
                if zh
                else f"- **{fact['description']}**: {quote} ([source]({evidence['url']}))"
            )
            claim_id = uuid5(context.run_id, f"claim:{fact['id']}")
            claims.append(
                {
                    "id": str(claim_id),
                    "claim_key": str(fact["key"]),
                    "text": quote,
                    "support_status": "GROUNDED",
                    "fact_id": fact["id"],
                    "evidence_id": evidence["verified_id"],
                    "citation_id": str(uuid5(claim_id, f"citation:{evidence['verified_id']}")),
                    "url": evidence["url"],
                }
            )
        if missing:
            prefix = "仍未获得足够证据" if zh else "Evidence is still insufficient"
            lines.append(f"\n{prefix}：" + "、".join(missing))
        if complete:
            quality = "COMPLETE"
            reason = "FACTS_COVERED"
        else:
            quality = "PARTIAL"
            reason = (
                "PROVIDER_FAILURE"
                if not claims and int(payload.get("provider_failures", 0)) > 0
                else "TIME_BUDGET"
                if context.clock.now() >= context.deadline_at
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
                "missing_fact_ids": [
                    fact["id"] for fact in required if fact["id"] not in accepted_by_fact
                ],
            },
            StepBudgetCost(BudgetPhase.SYNTHESIZE),
        )


__all__ = [
    "HeuristicIntentPlanner",
    "IntentPlanner",
    "ModelIntentPlanner",
    "SearchStepOperations",
]
