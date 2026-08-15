from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from sana.modules.answer.model_synthesizer import ConstrainedModelSynthesizer
from sana.modules.evidence.coverage import CoverageEvaluator
from sana.modules.evidence.domain import SourceAuthority
from sana.modules.model_gateway.domain import ModelInvocationContext, ModelResult
from sana.modules.search_planning.domain import FactRequirement, FactType
from sana.modules.shared.ids import TraceContext
from tests.test_modules.evidence.test_evidence_levels import NOW, grounded_evidence


class ParsingGateway:
    def __init__(self, text: str) -> None:
        self.text = text

    async def generate(self, role, messages, *, parser, **kwargs):
        return ModelResult("", "model", parsed=parser.parse(self.text))


class ForbiddenGateway:
    async def generate(self, *args, **kwargs):
        raise AssertionError("No model call is allowed without accepted evidence")


@pytest.mark.asyncio
async def test_valid_model_claim_gets_deterministic_full_lineage_citation() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="model-synth",
    )
    fact = FactRequirement("launch", FactType.CURRENT_VALUE, "launch date", "launch date")
    coverage = CoverageEvaluator().evaluate(
        evidence.tenant_id, evidence.run_id, fact_id, fact, (evidence,)
    )
    gateway = ParsingGateway(
        '{"claims":[{'
        f'"claim_key":"launch","text":"Launches August 14, 2026",'
        f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
    )
    invocation = ModelInvocationContext(
        evidence.tenant_id,
        evidence.run_id,
        uuid4(),
        "synthesize",
        uuid4(),
        1,
        TraceContext.create(),
        ("verify:sha256",),
    )

    result = await ConstrainedModelSynthesizer(gateway).synthesize(
        tenant_id=evidence.tenant_id,
        run_id=evidence.run_id,
        facts={fact_id: fact},
        coverage={fact_id: coverage},
        evidence=(evidence,),
        invocation_context=invocation,
        deadline=NOW + timedelta(seconds=5),
    )

    assert result.degraded is False
    assert result.answer.factual_traceability_rate == 1.0
    citation = result.answer.citations[0]
    assert citation.document_version_id == evidence.source.document_version_id
    assert citation.document_chunk_id == evidence.source.document_chunk_id
    assert citation.quote == evidence.candidate.quote


@pytest.mark.asyncio
async def test_cross_fact_evidence_mapping_falls_back_without_leaking_mapping() -> None:
    tenant_id, run_id, fact_a, fact_b = uuid4(), uuid4(), uuid4(), uuid4()
    evidence = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_a,
        source_identity="publisher.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="cross-fact",
    )
    facts = {
        fact_a: FactRequirement("a", FactType.CURRENT_VALUE, "value a", "value a"),
        fact_b: FactRequirement("b", FactType.CURRENT_VALUE, "value b", "value b"),
    }
    coverage = {
        key: CoverageEvaluator().evaluate(
            tenant_id,
            run_id,
            key,
            fact,
            (evidence,),
        )
        for key, fact in facts.items()
    }
    gateway = ParsingGateway(
        '{"claims":[{'
        f'"claim_key":"bad","text":"bad mapping","fact_id":"{fact_b}",'
        f'"evidence_ids":["{evidence.id}"]}}]}}'
    )
    invocation = ModelInvocationContext(
        tenant_id,
        run_id,
        uuid4(),
        "synthesize",
        uuid4(),
        1,
        TraceContext.create(),
        ("verify:sha256",),
    )

    result = await ConstrainedModelSynthesizer(gateway).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        facts=facts,
        coverage=coverage,
        evidence=(evidence,),
        invocation_context=invocation,
        deadline=NOW + timedelta(seconds=5),
    )

    assert result.degraded is True
    assert all(claim.fact_requirement_id != fact_b for claim in result.answer.claims)


@pytest.mark.asyncio
async def test_no_accepted_evidence_skips_model_and_returns_safe_empty_answer() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    fact = FactRequirement(
        "missing",
        FactType.CURRENT_VALUE,
        "missing value",
        "missing",
    )
    coverage = CoverageEvaluator().evaluate(
        tenant_id,
        run_id,
        fact_id,
        fact,
        (),
    )
    invocation = ModelInvocationContext(
        tenant_id,
        run_id,
        uuid4(),
        "synthesize",
        uuid4(),
        1,
        TraceContext.create(),
        ("verify:sha256",),
    )

    result = await ConstrainedModelSynthesizer(ForbiddenGateway()).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        facts={fact_id: fact},
        coverage={fact_id: coverage},
        evidence=(),
        invocation_context=invocation,
        deadline=NOW + timedelta(seconds=5),
    )

    assert result.degraded is False
    assert result.answer.claims == ()
    assert result.answer.citations == ()


@pytest.mark.asyncio
async def test_empty_model_claims_with_accepted_evidence_fall_back_to_citation() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="empty-claims",
    )
    fact = FactRequirement(
        "launch",
        FactType.CURRENT_VALUE,
        "launch date",
        "launch date",
    )
    coverage = CoverageEvaluator().evaluate(
        evidence.tenant_id,
        evidence.run_id,
        fact_id,
        fact,
        (evidence,),
    )
    invocation = ModelInvocationContext(
        evidence.tenant_id,
        evidence.run_id,
        uuid4(),
        "synthesize",
        uuid4(),
        1,
        TraceContext.create(),
        ("verify:sha256",),
    )

    result = await ConstrainedModelSynthesizer(
        ParsingGateway('{"claims":[]}')
    ).synthesize(
        tenant_id=evidence.tenant_id,
        run_id=evidence.run_id,
        facts={fact_id: fact},
        coverage={fact_id: coverage},
        evidence=(evidence,),
        invocation_context=invocation,
        deadline=NOW + timedelta(seconds=5),
    )

    assert result.degraded is True
    assert len(result.answer.claims) == 1
    assert len(result.answer.citations) == 1
