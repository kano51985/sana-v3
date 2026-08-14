import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.builder import EvidenceBuilder
from sana.modules.evidence.coverage import CoverageEvaluator, FactCoverage
from sana.modules.evidence.domain import (
    DiscoveryEvidence,
    EvidenceLevel,
    EvidenceVerdict,
    SourceAuthority,
    SupportType,
)
from sana.modules.evidence.verifier import EvidenceVerifier
from sana.modules.search_planning.domain import FactRequirement, FactType
from sana.modules.shared.ids import DeterministicIdFactory


NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def grounded_evidence(
    *,
    tenant_id: UUID,
    run_id: UUID,
    fact_id: UUID,
    source_identity: str,
    authority: SourceAuthority,
    seed: str,
):
    text = "The launch date is August 14, 2026. Additional context."
    version = DocumentVersion(
        id=uuid4(),
        tenant_id=tenant_id,
        document_id=uuid4(),
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        media_type="text/plain",
        language="en",
        fetched_at=NOW,
    )
    chunk = DocumentChunk(
        ordinal=0,
        text=text,
        text_hash=hashlib.sha256(text.encode()).hexdigest(),
        token_count=10,
        start_offset=0,
        end_offset=len(text),
    )
    candidate = EvidenceBuilder(DeterministicIdFactory(seed)).build(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_requirement_id=fact_id,
        document_version=version,
        document_chunk_id=uuid4(),
        document_chunk=chunk,
        document_id=version.document_id,
        source_url=f"https://{source_identity}/launch",
        source_identity=source_identity,
        support_type=SupportType.SUPPORTS,
        quote="August 14, 2026",
        quote_start_in_chunk=text.index("August"),
        candidate_score=0.9,
        authority=authority,
    )
    return EvidenceVerifier(
        DeterministicIdFactory(seed + "-verified"),
        verifier_version="fixture-v1",
    ).record(
        candidate,
        verdict=EvidenceVerdict.ACCEPTED,
        confidence=0.95,
        reason_codes=("exact_quote",),
        verified_at=NOW,
    )


def fact() -> FactRequirement:
    return FactRequirement(
        key="launch-date",
        fact_type=FactType.CURRENT_VALUE,
        description="Current launch date",
        subject="launch date",
    )


def test_l0_search_snippet_does_not_change_fact_coverage() -> None:
    fact_id = uuid4()
    discovery = DiscoveryEvidence(
        uuid4(),
        uuid4(),
        uuid4(),
        fact_id,
        "https://search.example/result",
        "Result",
        "The launch date might be August 14",
    )

    result = CoverageEvaluator().evaluate(
        discovery.tenant_id,
        discovery.run_id,
        fact_id,
        fact(),
        (),
        discovery=(discovery,),
    )

    assert discovery.level is EvidenceLevel.L0_DISCOVERY
    assert result.status is FactCoverage.OPEN
    assert result.level is None
    assert result.discovery_count == 1


def test_builder_requires_exact_quote_at_exact_document_offset() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="publisher.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="exact",
    )

    assert evidence.level is EvidenceLevel.L1_GROUNDED
    assert evidence.candidate.quote == "August 14, 2026"
    assert evidence.candidate.end_offset - evidence.candidate.start_offset == len(
        evidence.candidate.quote
    )


def test_search_hit_cannot_be_passed_as_a_document_version() -> None:
    discovery = DiscoveryEvidence(
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        "https://example.com",
        "title",
        "snippet",
    )

    with pytest.raises(TypeError, match="DocumentVersion"):
        EvidenceBuilder(DeterministicIdFactory("blocked")).build(
            tenant_id=uuid4(),
            run_id=uuid4(),
            fact_requirement_id=uuid4(),
            document_version=discovery,  # type: ignore[arg-type]
            document_chunk_id=uuid4(),
            document_chunk=object(),  # type: ignore[arg-type]
            document_id=uuid4(),
            source_url="https://example.com",
            source_identity="example.com",
            support_type=SupportType.SUPPORTS,
            quote="snippet",
            quote_start_in_chunk=0,
            candidate_score=0.5,
            authority=SourceAuthority.UNKNOWN,
        )


def test_official_source_promotes_fact_to_l2() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="official",
    )

    result = CoverageEvaluator().evaluate(
        tenant_id, run_id, fact_id, fact(), (evidence,)
    )

    assert result.status is FactCoverage.VERIFIED
    assert result.level is EvidenceLevel.L2_VERIFIED
    assert result.reason_codes == ("official_source",)


def test_two_independent_consistent_sources_promote_fact_to_l2() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = tuple(
        grounded_evidence(
            tenant_id=tenant_id,
            run_id=run_id,
            fact_id=fact_id,
            source_identity=source,
            authority=SourceAuthority.INDEPENDENT,
            seed=source,
        )
        for source in ("first.example", "second.example")
    )

    result = CoverageEvaluator().evaluate(tenant_id, run_id, fact_id, fact(), evidence)

    assert result.status is FactCoverage.VERIFIED
    assert result.reason_codes == ("two_independent_sources",)


def test_same_publisher_twice_remains_l1_covered() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = tuple(
        grounded_evidence(
            tenant_id=tenant_id,
            run_id=run_id,
            fact_id=fact_id,
            source_identity="syndicated.example",
            authority=SourceAuthority.INDEPENDENT,
            seed=f"same-{index}",
        )
        for index in range(2)
    )

    result = CoverageEvaluator().evaluate(tenant_id, run_id, fact_id, fact(), evidence)

    assert result.status is FactCoverage.COVERED
    assert result.level is EvidenceLevel.L1_GROUNDED
