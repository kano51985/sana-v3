from dataclasses import replace
from uuid import uuid4

from sana.modules.answer.citation_validator import CitationValidator
from sana.modules.answer.domain import (
    ClaimKind,
    ClaimSupport,
    ProposedClaim,
    UnsupportedClaimPolicy,
)
from sana.modules.answer.synthesizer import ClaimSynthesizer
from sana.modules.evidence.coverage import CoverageEvaluator
from sana.modules.evidence.domain import (
    DiscoveryEvidence,
    EvidenceVerdict,
    SourceAuthority,
)
from sana.modules.search_planning.domain import FactRequirement, FactType
from sana.modules.shared.ids import DeterministicIdFactory

from tests.test_modules.evidence.test_evidence_levels import grounded_evidence


def test_every_factual_claim_has_a_traceable_exact_quote_citation() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="citation",
    )
    fact = FactRequirement(
        key="launch-date",
        fact_type=FactType.CURRENT_VALUE,
        description="Current launch date",
        subject="launch date",
    )
    coverage = CoverageEvaluator().evaluate(
        tenant_id, run_id, fact_id, fact, (evidence,)
    )
    draft = ClaimSynthesizer(DeterministicIdFactory("claim")).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        proposals=(
            ProposedClaim(
                "launch-date",
                "The launch date is August 14, 2026.",
                fact_id,
                (evidence.id,),
            ),
        ),
        coverage_by_fact={fact_id: coverage},
    )

    validated = CitationValidator(
        DeterministicIdFactory("citation-id")
    ).validate(draft, {evidence.id: evidence})

    assert validated.factual_traceability_rate == 1.0
    assert len(validated.claims) == len(validated.citations) == 1
    assert validated.claims[0].support is ClaimSupport.VERIFIED
    citation = validated.citations[0]
    assert citation.verified_evidence_id == evidence.id
    assert citation.document_version_id == evidence.source.document_version_id
    assert citation.document_chunk_id == evidence.source.document_chunk_id
    assert citation.quote == evidence.candidate.quote
    assert citation.start_offset == evidence.candidate.start_offset
    assert citation.end_offset == evidence.candidate.end_offset


def test_search_snippet_cannot_generate_a_citation() -> None:
    tenant_id, run_id, fact_id, fake_evidence_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    discovery = DiscoveryEvidence(
        tenant_id,
        run_id,
        uuid4(),
        fact_id,
        "https://search.example/result",
        "Result",
        "Unfetched snippet",
    )
    claim = ClaimSynthesizer(DeterministicIdFactory("unsupported")).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        proposals=(
            ProposedClaim(
                "unfetched",
                "The snippet says this is true.",
                fact_id,
                (fake_evidence_id,),
            ),
        ),
        coverage_by_fact={},
    )
    claim = replace(
        claim,
        claims=(replace(claim.claims[0], evidence_ids=(fake_evidence_id,)),),
    )

    validated = CitationValidator(
        DeterministicIdFactory("blocked-citation")
    ).validate(claim, {fake_evidence_id: discovery})

    assert validated.citations == ()
    assert validated.claims[0].kind is ClaimKind.UNCERTAINTY
    assert validated.claims[0].support is ClaimSupport.UNCONFIRMED
    assert "尚未确认" in validated.claims[0].text
    assert validated.factual_traceability_rate == 1.0


def test_rejected_or_cross_tenant_evidence_is_removed() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    accepted = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="source.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="rejected",
    )
    rejected = replace(accepted, verdict=EvidenceVerdict.REJECTED)
    draft = ClaimSynthesizer(DeterministicIdFactory("rejected-claim")).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        proposals=(ProposedClaim("claim", "Unsupported fact.", fact_id),),
        coverage_by_fact={},
    )
    draft = replace(
        draft,
        claims=(replace(draft.claims[0], evidence_ids=(rejected.id,)),),
    )

    validated = CitationValidator(
        DeterministicIdFactory("rejected-citation")
    ).validate(
        draft,
        {rejected.id: rejected},
        unsupported_policy=UnsupportedClaimPolicy.DROP,
    )

    assert validated.claims == ()
    assert validated.citations == ()
    assert {issue.code for issue in validated.issues} == {
        "invalid_evidence_mapping",
        "unsupported_factual_claim",
    }


def test_invalid_model_evidence_ids_are_filtered_before_validation() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="source.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="filter",
    )
    fact = FactRequirement(
        key="launch-date",
        fact_type=FactType.CURRENT_VALUE,
        description="Current launch date",
        subject="launch date",
    )
    coverage = CoverageEvaluator().evaluate(
        tenant_id, run_id, fact_id, fact, (evidence,)
    )
    draft = ClaimSynthesizer(DeterministicIdFactory("filter-claim")).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        proposals=(
            ProposedClaim(
                "claim",
                "Grounded fact.",
                fact_id,
                (uuid4(), evidence.id, evidence.id),
            ),
        ),
        coverage_by_fact={fact_id: coverage},
    )

    assert draft.claims[0].evidence_ids == (evidence.id,)


def test_validator_recalculates_an_overstated_support_level() -> None:
    tenant_id, run_id, fact_id = uuid4(), uuid4(), uuid4()
    evidence = grounded_evidence(
        tenant_id=tenant_id,
        run_id=run_id,
        fact_id=fact_id,
        source_identity="single.example",
        authority=SourceAuthority.INDEPENDENT,
        seed="overstated",
    )
    fact = FactRequirement(
        key="launch-date",
        fact_type=FactType.CURRENT_VALUE,
        description="Current launch date",
        subject="launch date",
    )
    coverage = CoverageEvaluator().evaluate(
        tenant_id, run_id, fact_id, fact, (evidence,)
    )
    draft = ClaimSynthesizer(DeterministicIdFactory("overstated-claim")).synthesize(
        tenant_id=tenant_id,
        run_id=run_id,
        proposals=(
            ProposedClaim("claim", "Grounded fact.", fact_id, (evidence.id,)),
        ),
        coverage_by_fact={fact_id: coverage},
    )
    draft = replace(
        draft,
        claims=(replace(draft.claims[0], support=ClaimSupport.VERIFIED),),
    )

    validated = CitationValidator(
        DeterministicIdFactory("overstated-citation")
    ).validate(draft, {evidence.id: evidence})

    assert validated.claims[0].support is ClaimSupport.GROUNDED
    assert "support_recalculated" in {issue.code for issue in validated.issues}
