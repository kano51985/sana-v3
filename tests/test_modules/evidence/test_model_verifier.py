from __future__ import annotations

import hashlib
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
