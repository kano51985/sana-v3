from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from sana.modules.answer.model_synthesizer import (
    ConstrainedModelSynthesizer,
    ProposedClaimParser,
)
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


def test_synthesizer_prompt_nests_accepted_evidence_under_owning_fact() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="nested-prompt",
    )
    fact = FactRequirement("launch", FactType.CURRENT_VALUE, "launch", "launch")
    coverage = CoverageEvaluator().evaluate(
        evidence.tenant_id,
        evidence.run_id,
        fact_id,
        fact,
        (evidence,),
    )

    messages = ConstrainedModelSynthesizer._messages(
        {fact_id: fact},
        {fact_id: coverage},
        (evidence,),
    )
    payload = json.loads(messages[1].content)

    assert "evidence" not in payload
    assert payload["facts"][0]["accepted_evidence"] == [
        {"evidence_id": str(evidence.id), "quote": evidence.candidate.quote}
    ]


def test_claim_parser_restores_only_grounded_requested_numeric_identifier() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="self-contained",
    )
    parser = ProposedClaimParser(
        {
            fact_id: FactRequirement(
                "launch_2026",
                FactType.CURRENT_VALUE,
                "Launch year 2026",
                "launch",
            )
        },
        (evidence,),
    )

    claims = parser.parse(
        '{"claims":[{'
        '"claim_key":"launch","text":"Launches August 14",'
        f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
    )

    assert claims[0].text == "2026: Launches August 14"


def test_claim_parser_rejects_ungrounded_missing_numeric_identifier() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="ungrounded-identifier",
    )
    parser = ProposedClaimParser(
        {
            fact_id: FactRequirement(
                "http_404",
                FactType.CURRENT_VALUE,
                "HTTP status 404 reason phrase",
                "HTTP",
            )
        },
        (evidence,),
    )

    with pytest.raises(ValueError, match="not grounded"):
        parser.parse(
            '{"claims":[{'
            '"claim_key":"http","text":"Not Found",'
            f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
        )


def test_claim_parser_restores_grounded_symbolic_identifier() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="official.example",
        authority=SourceAuthority.OFFICIAL,
        seed="symbolic-identifier",
    )
    evidence = replace(
        evidence,
        candidate=replace(
            evidence.candidate,
            quote="Read Uncommitted can permit dirty reads.",
            quote_hash=hashlib.sha256(
                b"Read Uncommitted can permit dirty reads."
            ).hexdigest(),
            start_offset=0,
            end_offset=len("Read Uncommitted can permit dirty reads."),
        ),
    )
    parser = ProposedClaimParser(
        {
            fact_id: FactRequirement(
                "read_uncommitted_anomalies",
                FactType.BACKGROUND,
                "Anomaly guarantees for Read Uncommitted",
                "Read Uncommitted",
            )
        },
        (evidence,),
    )

    claims = parser.parse(
        '{"claims":[{'
        '"claim_key":"dirty-read","text":"Dirty reads can occur",'
        f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
    )

    assert claims[0].text == "Read Uncommitted: Dirty reads can occur"


def test_rfc_identifier_is_grounded_by_reviewed_source_url_as_one_unit() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="rfc-editor.org",
        authority=SourceAuthority.OFFICIAL,
        seed="rfc-source-identifier",
    )
    quote = 'Offsets "Z" and "+00:00" both identify UTC.'
    evidence = replace(
        evidence,
        candidate=replace(
            evidence.candidate,
            source=replace(
                evidence.source,
                url="https://www.rfc-editor.org/rfc/rfc3339.txt",
            ),
            quote=quote,
            quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
            start_offset=0,
            end_offset=len(quote),
        ),
    )
    parser = ProposedClaimParser(
        {
            fact_id: FactRequirement(
                "rfc3339_plus0000_semantics",
                FactType.COMPARISON,
                "Semantics of '+00:00' in RFC 3339",
                "RFC 3339 '+00:00' offset",
            )
        },
        (evidence,),
    )

    claims = parser.parse(
        '{"claims":[{'
        '"claim_key":"offset","text":"+00:00 identifies UTC",'
        f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
    )

    assert claims[0].text == "RFC 3339: +00:00 identifies UTC"


def test_symbolic_identifier_preserves_requested_case() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="postgresql.org",
        authority=SourceAuthority.OFFICIAL,
        seed="symbolic-case",
    )
    quote = "Read uncommitted is treated as Read committed."
    evidence = replace(
        evidence,
        candidate=replace(
            evidence.candidate,
            quote=quote,
            quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
            start_offset=0,
            end_offset=len(quote),
        ),
    )
    parser = ProposedClaimParser(
        {
            fact_id: FactRequirement(
                "read_uncommitted",
                FactType.COMPARISON,
                "Behavior of Read Uncommitted",
                "Read Uncommitted",
            )
        },
        (evidence,),
    )

    claims = parser.parse(
        '{"claims":[{'
        '"claim_key":"level","text":"Read uncommitted maps to Read committed",'
        f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
    )

    assert claims[0].text.startswith("Read Uncommitted:")


def test_rfc3339_plus_zero_example_rejects_a_z_suffixed_timestamp() -> None:
    fact_id = uuid4()
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=fact_id,
        source_identity="rfc-editor.org",
        authority=SourceAuthority.OFFICIAL,
        seed="rfc3339-relationship",
    )
    quote = '"Z" or "+00:00" both identify UTC; 1985-04-12T23:20:50.52Z.'
    evidence = replace(
        evidence,
        candidate=replace(
            evidence.candidate,
            source=replace(
                evidence.source,
                url="https://www.rfc-editor.org/rfc/rfc3339.txt",
            ),
            quote=quote,
            quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
            start_offset=0,
            end_offset=len(quote),
        ),
    )
    fact = FactRequirement(
        "rfc3339_example_plus0000",
        FactType.BACKGROUND,
        "Example of an RFC 3339 timestamp using '+00:00'",
        "RFC 3339 '+00:00' example",
    )
    parser = ProposedClaimParser({fact_id: fact}, (evidence,))

    with pytest.raises(ValueError, match=r"ending \+00:00"):
        parser.parse(
            '{"claims":[{'
            '"claim_key":"example","text":"Example: 1985-04-12T23:20:50.52Z",'
            f'"fact_id":"{fact_id}","evidence_ids":["{evidence.id}"]}}]}}'
        )

    assert ConstrainedModelSynthesizer._derived_fallback_text(
        fact,
        (evidence.id,),
        {evidence.id: evidence},
    ) == "1985-04-12T23:20:50.52+00:00"


@pytest.mark.parametrize(
    ("fact", "quote", "url", "expected"),
    [
        (
            FactRequirement(
                "json_media_type",
                FactType.CURRENT_VALUE,
                "The registered media type for JSON",
                "JSON media type",
            ),
            "Type name: application\nSubtype name: json\nPublished specification: RFC 8259",
            "https://www.iana.org/assignments/media-types/application/json",
            "application/json",
        ),
        (
            FactRequirement(
                "tls13_rfc",
                FactType.CURRENT_VALUE,
                "The RFC that specifies TLS 1.3",
                "TLS 1.3",
            ),
            "Request for Comments: 8446; TLS Protocol Version 1.3",
            "https://www.rfc-editor.org/rfc/rfc8446.txt",
            "TLS 1.3 is specified by RFC 8446.",
        ),
        (
            FactRequirement(
                "sha256_digest_length",
                FactType.CURRENT_VALUE,
                "SHA-256 digest length in bits",
                "SHA-256",
            ),
            "The SHA-224 and SHA-256 algorithms produce 224-bit and 256-bit message digests",
            "https://www.rfc-editor.org/rfc/rfc6234.txt",
            "The SHA-256 digest length is 256 bits.",
        ),
    ],
)
def test_reviewed_relationships_have_safe_deterministic_fallbacks(
    fact: FactRequirement,
    quote: str,
    url: str,
    expected: str,
) -> None:
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=uuid4(),
        source_identity="standards.example",
        authority=SourceAuthority.OFFICIAL,
        seed=expected,
    )
    evidence = replace(
        evidence,
        candidate=replace(
            evidence.candidate,
            source=replace(evidence.source, url=url),
            quote=quote,
            quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
            start_offset=0,
            end_offset=len(quote),
        ),
    )

    derived = ConstrainedModelSynthesizer._derived_fallback_text(
        fact,
        (evidence.id,),
        {evidence.id: evidence},
    )

    assert derived == expected
    ProposedClaimParser._validate_requested_relationship(fact, derived)


@pytest.mark.parametrize(
    ("fact", "quote", "expected"),
    (
        (
            FactRequirement(
                "git_object_types",
                FactType.BACKGROUND,
                "What are the four types of objects in Git's object model?",
                "Git",
            ),
            "There are 4 types of objects: commits, trees, blobs, and tag objects.",
            "Git has four object types: commit, tree, blob, and tag.",
        ),
        (
            FactRequirement(
                "blob_purpose",
                FactType.BACKGROUND,
                "What is the purpose of the blob object in Git?",
                "Git",
            ),
            "A blob object contains a file's contents.",
            "A Git blob object contains a file's contents.",
        ),
        (
            FactRequirement(
                "tree_purpose",
                FactType.BACKGROUND,
                "What is the purpose of the tree object in Git?",
                "Git",
            ),
            "A tree is how Git represents a directory. It can contain files or "
            "other trees (which are subdirectories).",
            "A Git tree object represents a directory and can contain files or subtrees.",
        ),
        (
            FactRequirement(
                "commit_purpose",
                FactType.BACKGROUND,
                "What is the purpose of the commit object in Git?",
                "Git",
            ),
            "A commit contains these required fields: a top-level tree, parent "
            "commit IDs, author and committer information, and a commit message",
            "A Git commit object records its top-level tree, parent commits, author "
            "and committer metadata, and commit message.",
        ),
        (
            FactRequirement(
                "tag_purpose",
                FactType.BACKGROUND,
                "What is the purpose of the tag object in Git?",
                "Git",
            ),
            "Tag objects contain these required fields: the ID and type of the "
            "object it references, tagger data, and a tag message, similar to a "
            "commit message",
            "A Git tag object records the referenced object's ID and type, tagger "
            "metadata, and a tag message.",
        ),
    ),
)
def test_reviewed_git_relationships_have_safe_fallbacks(
    fact: FactRequirement,
    quote: str,
    expected: str,
) -> None:
    evidence = grounded_evidence(
        tenant_id=uuid4(),
        run_id=uuid4(),
        fact_id=uuid4(),
        source_identity="git-scm.com",
        authority=SourceAuthority.OFFICIAL,
        seed=expected,
    )
    evidence = replace(
        evidence,
        candidate=replace(
            evidence.candidate,
            source=replace(
                evidence.source,
                url="https://git-scm.com/docs/gitdatamodel.html",
            ),
            quote=quote,
            quote_hash=hashlib.sha256(quote.encode()).hexdigest(),
            start_offset=0,
            end_offset=len(quote),
        ),
    )

    derived = ConstrainedModelSynthesizer._derived_fallback_text(
        fact,
        (evidence.id,),
        {evidence.id: evidence},
    )

    assert derived == expected
    ProposedClaimParser._validate_requested_relationship(fact, derived)


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
