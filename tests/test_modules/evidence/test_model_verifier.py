from __future__ import annotations

import hashlib
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

    assert len(result.evidence) == 1
    assert result.evidence[0].candidate.id == item.id
    assert result.evidence[0].verdict is EvidenceVerdict.REJECTED
    assert result.evidence[0].reason_codes == (
        "exact_source_span",
        "insufficient_direct_support",
    )
