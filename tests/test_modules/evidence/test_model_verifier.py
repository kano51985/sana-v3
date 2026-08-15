from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.candidate_selector import SelectedCandidate
from sana.modules.evidence.domain import EvidenceVerdict, SourceAuthority
from sana.modules.evidence.model_verifier import ModelEvidenceVerifier
from sana.modules.model_gateway.domain import ModelInvocationContext, ModelResult
from sana.modules.search_planning.domain import FactRequirement, FactType
from sana.modules.shared.ids import TraceContext


NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)


class ParsingGateway:
    def __init__(self, text: str) -> None:
        self.text = text

    async def generate(self, role, messages, *, parser, **kwargs):
        return ModelResult("", "model", parsed=parser.parse(self.text))


class ForbiddenGateway:
    async def generate(self, *args, **kwargs):
        raise AssertionError("exact official values must not require a model call")


def candidate() -> SelectedCandidate:
    tenant_id, document_id = uuid4(), uuid4()
    text = "Apex Legends current version is 27.1 according to the patch notes."
    version = DocumentVersion(
        uuid4(),
        tenant_id,
        document_id,
        hashlib.sha256(text.encode()).hexdigest(),
        text,
        "text/plain",
        "en",
        NOW,
    )
    chunk = DocumentChunk(
        0,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        12,
        0,
        len(text),
    )
    return SelectedCandidate(
        uuid4(),
        uuid4(),
        FactRequirement("version", FactType.VERSION, "current version", "Apex Legends"),
        document_id,
        version,
        uuid4(),
        chunk,
        "https://www.ea.com/games/apex",
        "Patch",
        "ea.com",
        SourceAuthority.OFFICIAL,
        text,
        0.9,
    )


def context(item: SelectedCandidate) -> ModelInvocationContext:
    return ModelInvocationContext(
        item.version.tenant_id,
        uuid4(),
        uuid4(),
        "verify",
        uuid4(),
        1,
        TraceContext.create(),
        ("extract:sha256",),
    )


@pytest.mark.asyncio
async def test_model_verdict_is_rebuilt_through_exact_span_gate() -> None:
    item = candidate()
    quote = "current version is 27.1"
    gateway = ParsingGateway(
        '{"verdicts":[{'
        f'"fact_id":"{item.fact_id}","candidate_id":"{item.id}",'
        f'"support_type":"SUPPORTS","quote":"{quote}","confidence":0.93,'
        '"reason_codes":["explicit_value"]}]}'
    )
    invocation = context(item)

    result = await ModelEvidenceVerifier(gateway).verify(
        (item,),
        invocation_context=invocation,
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].candidate.quote == quote
    assert item.version.text[
        result.evidence[0].candidate.start_offset : result.evidence[0].candidate.end_offset
    ] == quote


@pytest.mark.asyncio
async def test_forged_quote_cannot_become_accepted_model_evidence() -> None:
    item = candidate()
    gateway = ParsingGateway(
        '{"verdicts":[{'
        f'"fact_id":"{item.fact_id}","candidate_id":"{item.id}",'
        '"support_type":"SUPPORTS","quote":"forged value 99",'
        '"confidence":1,"reason_codes":["explicit_value"]}]}'
    )

    result = await ModelEvidenceVerifier(gateway).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is True
    assert all(evidence.candidate.quote != "forged value 99" for evidence in result.evidence)
    assert all(evidence.confidence <= 0.49 for evidence in result.evidence)


@pytest.mark.asyncio
async def test_model_omission_persists_a_rejected_candidate_audit() -> None:
    item = candidate()
    result = await ModelEvidenceVerifier(ParsingGateway('{"verdicts":[]}')).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].candidate.id == item.id
    assert result.evidence[0].verifier_version == "deepseek-verifier-v1"


def test_verifier_prompt_groups_candidates_by_fact_and_requires_compact_output() -> None:
    item = candidate()
    messages = ModelEvidenceVerifier._messages((item,))
    payload = json.loads(messages[1].content)

    assert "candidates" not in payload
    assert payload["facts"][0]["fact_id"] == str(item.fact_id)
    assert payload["facts"][0]["candidates"][0]["candidate_id"] == str(item.id)
    assert "at most one strongest verdict per fact" in messages[0].content


def explicit_http_candidate() -> SelectedCandidate:
    base = candidate()
    text = '404,Not Found,"[RFC9110, Section 15.5.5]"'
    version = replace(
        base.version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    chunk = DocumentChunk(
        0,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        8,
        0,
        len(text),
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "http_404_reason_phrase",
            FactType.CURRENT_VALUE,
            "The English reason phrase for HTTP status code 404",
            "HTTP",
        ),
        version=version,
        chunk_id=uuid4(),
        chunk=chunk,
        url=(
            "https://www.iana.org/assignments/http-status-codes/"
            "http-status-codes-1.csv"
        ),
        source_identity="iana.org",
        quote=text,
        score=0.84,
    )


def explicit_json_terms_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "A JSON value can be one of the following three literal names: "
        "false, null, and true."
    )
    version = replace(
        base.version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "json_literals_three",
            FactType.BACKGROUND,
            "The three JSON literals are: true, false, and null.",
            "JSON standard",
        ),
        version=version,
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            16,
            0,
            len(text),
        ),
        url="https://www.rfc-editor.org/rfc/rfc8259.html",
        source_identity="rfc-editor.org",
        quote=text,
        score=0.94,
    )


def http_method_registry_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "Method Name\nSafe\nIdempotent\nReference\n"
        "DELETE\nno\nyes\n[RFC9110]\nGET\nyes\nyes\n[RFC9110]"
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "get_safe",
            FactType.BACKGROUND,
            "Whether HTTP GET is safe according to HTTP semantics",
            "HTTP GET method",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            12,
            0,
            len(text),
        ),
        url="https://www.iana.org/assignments/http-methods/http-methods.xhtml",
        source_identity="iana.org",
        quote=text,
        score=0.88,
    )


def media_type_registry_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "Type name: application\nSubtype name: json\n"
        "Published specification: RFC 8259"
    )
    selected_quote = "Availability considerations: See RFC 8259"
    text = f"{text}\n{selected_quote}"
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "json_media_type",
            FactType.CURRENT_VALUE,
            "The registered media type for JSON",
            "JSON media type",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            12,
            0,
            len(text),
        ),
        url="https://www.iana.org/assignments/media-types/application/json",
        source_identity="iana.org",
        quote=selected_quote,
        score=0.34,
    )


def sha_digest_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "The SHA-224 and SHA-256 algorithms produce 224-bit and 256-bit * "
        "message digests for a given data stream."
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "sha256_digest_length_bits",
            FactType.BACKGROUND,
            "SHA-256 digest length in bits",
            "SHA-256",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            18,
            0,
            len(text),
        ),
        url="https://www.rfc-editor.org/rfc/rfc6234.txt",
        source_identity="rfc-editor.org",
        quote=text,
        score=0.92,
    )


def tls_rfc_candidate() -> SelectedCandidate:
    base = candidate()
    text = (
        "Request for Comments: 8446 Mozilla\nCategory: Standards Track\n"
        "The Transport Layer Security (TLS) Protocol Version 1.3\nAbstract"
    )
    return replace(
        base,
        fact_id=uuid4(),
        fact=FactRequirement(
            "tls13_rfc",
            FactType.VERSION,
            "Which RFC specifies TLS 1.3",
            "TLS 1.3",
        ),
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            18,
            0,
            len(text),
        ),
        url="https://www.rfc-editor.org/rfc/rfc8446.txt",
        source_identity="rfc-editor.org",
        quote=text,
        score=0.92,
    )


def reviewed_text_candidate(
    text: str,
    fact: FactRequirement,
    url: str,
    *,
    fact_id=None,
) -> SelectedCandidate:
    base = candidate()
    return replace(
        base,
        fact_id=fact_id or uuid4(),
        fact=fact,
        version=replace(
            base.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk_id=uuid4(),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            32,
            0,
            len(text),
        ),
        url=url,
        source_identity=("postgresql.org" if "postgresql" in url else "rfc-editor.org"),
        quote=text,
        score=0.92,
    )


@pytest.mark.asyncio
async def test_exact_official_numeric_value_skips_model_verification() -> None:
    item = explicit_http_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].candidate.quote == "404,Not Found"
    assert result.evidence[0].verifier_version == "deterministic-explicit-value-v1"


@pytest.mark.asyncio
async def test_exact_official_term_list_skips_model_verification() -> None:
    item = explicit_json_terms_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].verifier_version == "deterministic-explicit-terms-v1"
    assert all(
        value in result.evidence[0].candidate.quote
        for value in ("true", "false", "null")
    )


@pytest.mark.asyncio
async def test_reviewed_registry_table_skips_model_verification() -> None:
    item = http_method_registry_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert result.evidence[0].verifier_version == "deterministic-registry-table-v1"
    assert "GET\nyes\nyes" in result.evidence[0].candidate.quote


@pytest.mark.asyncio
async def test_reviewed_media_registry_row_skips_model_verification() -> None:
    item = media_type_registry_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert "Type name: application" in result.evidence[0].candidate.quote
    assert "Subtype name: json" in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-registry-media-v1"


@pytest.mark.asyncio
async def test_reviewed_sha_digest_statement_skips_model_verification() -> None:
    item = sha_digest_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert "SHA-256 algorithms produce" in result.evidence[0].candidate.quote
    assert "256-bit" in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-sha-digest-v1"


@pytest.mark.asyncio
async def test_reviewed_rfc_header_skips_model_verification() -> None:
    item = tls_rfc_candidate()

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].verdict is EvidenceVerdict.ACCEPTED
    assert "Comments: 8446" in result.evidence[0].candidate.quote
    assert "TLS) Protocol Version 1.3" in result.evidence[0].candidate.quote
    assert result.evidence[0].verifier_version == "deterministic-rfc-title-v1"


@pytest.mark.asyncio
async def test_reviewed_rfc3339_can_bind_two_exact_premises_for_derived_example() -> None:
    fact_id = uuid4()
    fact = FactRequirement(
        "rfc3339_example_plus0000",
        FactType.BACKGROUND,
        "Provide an example of an RFC 3339 timestamp using '+00:00' for UTC.",
        "RFC 3339 '+00:00' example",
    )
    url = "https://www.rfc-editor.org/rfc/rfc3339.txt"
    equivalence = reviewed_text_candidate(
        'This differs\nsemantically from an offset of "Z" or "+00:00", '
        "which imply that UTC\nis the preferred reference point for the specified time.",
        fact,
        url,
        fact_id=fact_id,
    )
    example = reviewed_text_candidate(
        "5.8. Examples\n1985-04-12T23:20:50.52Z",
        fact,
        url,
        fact_id=fact_id,
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (equivalence, example),
        invocation_context=context(equivalence),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    accepted = [
        item for item in result.evidence if item.verdict is EvidenceVerdict.ACCEPTED
    ]
    assert result.degraded is False
    assert len(accepted) == 2
    assert {item.verifier_version for item in accepted} == {
        "deterministic-rfc3339-utc-v1"
    }


@pytest.mark.asyncio
async def test_reviewed_postgresql_table_is_narrowed_to_requested_level() -> None:
    table = (
        "Isolation Level Dirty Read Nonrepeatable Read Phantom Read "
        "Serialization Anomaly "
        "Read uncommitted Allowed, but not in PG Possible Possible Possible "
        "Read committed Not possible Possible Possible Possible "
        "Repeatable read Not possible Not possible Allowed, but not in PG Possible "
        "Serializable Not possible Not possible Not possible Not possible"
    )
    fact = FactRequirement(
        "isolation_level_repeatable_read",
        FactType.COMPARISON,
        "PostgreSQL anomaly guarantees for Repeatable Read isolation",
        "Repeatable Read",
    )
    item = reviewed_text_candidate(
        table,
        fact,
        "https://www.postgresql.org/docs/current/transaction-iso.html",
    )

    result = await ModelEvidenceVerifier(ForbiddenGateway()).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.degraded is False
    assert result.evidence[0].candidate.quote.startswith("Repeatable read")
    assert "Read uncommitted" not in result.evidence[0].candidate.quote
    assert (
        result.evidence[0].verifier_version
        == "deterministic-postgresql-isolation-v1"
    )


def test_registry_verification_is_bound_to_exact_reviewed_page() -> None:
    item = replace(
        http_method_registry_candidate(),
        url="https://www.iana.org/assignments/http-methods/unreviewed.xhtml",
    )

    assert ModelEvidenceVerifier._deterministic_registry_boolean(item) is None


@pytest.mark.asyncio
async def test_incomplete_official_term_list_still_uses_model() -> None:
    item = explicit_json_terms_candidate()
    text = "A JSON value can be the literal true."
    item = replace(
        item,
        version=replace(
            item.version,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
        ),
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            8,
            0,
            len(text),
        ),
        quote=text,
    )

    result = await ModelEvidenceVerifier(
        ParsingGateway('{"verdicts":[]}')
    ).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED


@pytest.mark.asyncio
async def test_numeric_mention_without_adjacent_value_still_uses_model() -> None:
    item = explicit_http_candidate()
    text = "Heuristically cacheable codes include 404, 405, and 410."
    version = replace(
        item.version,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
    )
    item = replace(
        item,
        version=version,
        chunk=DocumentChunk(
            0,
            text,
            hashlib.sha256(text.encode()).hexdigest(),
            8,
            0,
            len(text),
        ),
        quote=text,
    )

    result = await ModelEvidenceVerifier(
        ParsingGateway('{"verdicts":[]}')
    ).verify(
        (item,),
        invocation_context=context(item),
        deadline=NOW + timedelta(seconds=5),
        verified_at=NOW,
    )

    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED

    assert len(result.evidence) == 1
    assert result.evidence[0].candidate.id == item.id
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].reason_codes == (
        "exact_source_span",
        "insufficient_direct_support",
    )
