"""Network-free Worker adapters for Docker Shadow Campaign fault injection."""

from __future__ import annotations

from dataclasses import replace
import hashlib
from html import escape
import json
from urllib.parse import parse_qs, quote, urlsplit

from sana.app.search_operations import HeuristicIntentPlanner
from sana.modules.content.domain import FetchArtifact, FetchRequest, FetchStatus
from sana.modules.discovery.domain import (
    DiscoveryQuery,
    ProviderMetrics,
    ProviderResponse,
    SearchHit,
)
from sana.modules.model_gateway.domain import ModelResult, ModelRole
from sana.modules.model_gateway.service import FakeModelGateway
from sana.modules.orchestration.domain import SearchMode
from sana.modules.search_planning.query_compiler import QueryCompiler
from sana.modules.search_planning.domain import NormalizedIntent
from sana.modules.shared.clock import Clock


FIXTURE_MODEL = "shadow-offline-fixture-v1"
FIXTURE_HOST = "shadow-fixture.invalid"
_NO_ANSWER_TERMS = (
    "internal codename",
    "private parameter weights",
    "unreleased",
    "unannounced",
    "内部代号",
    "尚未公开",
    "未公开模型",
)


def _current_request(messages) -> str:
    content = messages[-1].content
    marker = "Current request:\n"
    if marker in content:
        content = content.split(marker, 1)[1]
    return content.split("\nAllowed context summary", 1)[0].strip()


def _intent_payload(intent: NormalizedIntent) -> dict[str, object]:
    return {
        "entity": intent.entity,
        "aliases": list(intent.aliases),
        "locale": intent.locale,
        "requires_comparison": intent.requires_comparison,
        "requires_complete_sources": intent.requires_complete_sources,
        "facts": [
            {
                "key": fact.key,
                "fact_type": fact.fact_type.value,
                "description": fact.description,
                "subject": fact.subject,
                "required": fact.required,
                "freshness": fact.freshness.value,
                "consequence": fact.consequence.value,
                "preferred_source_kinds": list(fact.preferred_source_kinds),
            }
            for fact in intent.facts
        ],
    }


class ShadowFixtureModelGateway(FakeModelGateway[object]):
    """Dynamic fixture gateway that performs zero provider calls and token billing."""

    def __init__(self) -> None:
        super().__init__([])
        self._planner = HeuristicIntentPlanner(QueryCompiler().policy.version)

    async def generate(self, role: ModelRole, messages, **kwargs) -> ModelResult:
        self.calls.append((role, messages))
        parser = kwargs.get("parser")
        if parser is None:
            raise AssertionError("Shadow fixture requires a structured-output parser")
        if role is ModelRole.PLANNER:
            request = _current_request(messages)
            planned = await self._planner.plan(
                request,
                mode=SearchMode.RESEARCH,
                deadline=kwargs["deadline"],
                max_llm_calls=0,
                invocation_context=kwargs.get("invocation_context"),
            )
            intent = planned.intent
            folded = request.casefold()
            if any(term in folded for term in _NO_ANSWER_TERMS):
                intent = replace(
                    intent,
                    entity="shadow-no-answer",
                    aliases=(),
                    facts=tuple(
                        replace(
                            fact,
                            description="shadow-no-answer evidence gap",
                            subject="shadow-no-answer",
                        )
                        for fact in intent.facts
                    ),
                )
            text = json.dumps(
                _intent_payload(intent),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif role is ModelRole.VERIFIER:
            payload = json.loads(messages[-1].content)
            text = json.dumps(
                {
                    "verdicts": [
                        {
                            "fact_id": item["fact_id"],
                            "candidate_id": item["candidate_id"],
                            "support_type": "SUPPORTS",
                            "quote": item["quote"],
                            "confidence": 0.99,
                            "reason_codes": ["direct_support"],
                        }
                        for item in payload["candidates"]
                    ]
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        elif role is ModelRole.SYNTHESIZER:
            payload = json.loads(messages[-1].content)
            evidence_by_fact: dict[str, list[dict[str, str]]] = {}
            for item in payload["evidence"]:
                evidence_by_fact.setdefault(item["fact_id"], []).append(item)
            claims = []
            for index, fact in enumerate(payload["facts"], start=1):
                evidence = evidence_by_fact.get(fact["fact_id"], [])
                if not evidence:
                    continue
                claims.append(
                    {
                        "claim_key": f"offline-fixture-{index}",
                        "text": evidence[0]["quote"],
                        "fact_id": fact["fact_id"],
                        "evidence_ids": [item["evidence_id"] for item in evidence],
                    }
                )
            text = json.dumps(
                {"claims": claims},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        else:  # pragma: no cover - fixture is wired only to these three roles
            raise AssertionError(f"Unsupported Shadow fixture model role: {role}")
        return ModelResult(
            text=text,
            model=FIXTURE_MODEL,
            parsed=parser.parse(text),
            provider_calls=0,
        )


class ShadowFixtureSearchProvider:
    name = "fixture"

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def search(
        self,
        query: DiscoveryQuery,
        *,
        timeout_seconds: float,
    ) -> ProviderResponse:
        del timeout_seconds
        if "shadow-no-answer" in query.text.casefold():
            return ProviderResponse(
                self.name,
                query.key,
                (),
                ProviderMetrics(0),
            )
        url = (
            f"https://{FIXTURE_HOST}/evidence"
            f"?query={quote(query.text, safe='')}&key={quote(query.key, safe='')}"
        )
        hit = SearchHit(
            self.name,
            query.key,
            1,
            url,
            url,
            f"Offline fixture evidence: {query.text}",
            query.text,
            1.0,
            self._clock.now(),
        )
        return ProviderResponse(
            self.name,
            query.key,
            (hit,),
            ProviderMetrics(0, raw_hit_count=1),
        )


class ShadowFixtureContentFetcher:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def fetch(self, request: FetchRequest) -> FetchArtifact:
        parsed = urlsplit(request.url)
        if parsed.scheme != "https" or parsed.hostname != FIXTURE_HOST:
            raise ValueError("Offline fixture fetcher rejected a non-fixture URL")
        query = parse_qs(parsed.query, strict_parsing=True)["query"][0]
        body = (
            "<!doctype html><html><head><title>Shadow Offline Fixture</title></head>"
            f"<body><main><h1>{escape(query)}</h1>"
            f"<p>Deterministic offline fixture evidence for {escape(query)}. "
            f"The verified fixture statement is {escape(query)}.</p></main></body></html>"
        ).encode("utf-8")
        return FetchArtifact(
            request.url,
            request.url,
            FetchStatus.SUCCEEDED,
            200,
            "text/html; charset=utf-8",
            body,
            hashlib.sha256(body).hexdigest(),
            self._clock.now(),
            response_headers={"content-type": "text/html; charset=utf-8"},
        )

    async def aclose(self) -> None:
        return None


__all__ = [
    "FIXTURE_HOST",
    "FIXTURE_MODEL",
    "ShadowFixtureContentFetcher",
    "ShadowFixtureModelGateway",
    "ShadowFixtureSearchProvider",
]
