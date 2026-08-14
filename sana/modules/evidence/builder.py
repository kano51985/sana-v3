"""Create L1 candidates only from exact DocumentVersion/Chunk spans."""

from __future__ import annotations

import hashlib
from uuid import UUID

from sana.modules.content.domain import DocumentChunk, DocumentVersion
from sana.modules.evidence.domain import (
    EvidenceCandidate,
    EvidenceSource,
    SourceAuthority,
    SupportType,
)
from sana.modules.shared.ids import IdFactory


class EvidenceBuilder:
    def __init__(self, id_factory: IdFactory) -> None:
        self._ids = id_factory

    def build(
        self,
        *,
        tenant_id: UUID,
        run_id: UUID,
        fact_requirement_id: UUID,
        document_version: DocumentVersion,
        document_chunk_id: UUID,
        document_chunk: DocumentChunk,
        document_id: UUID,
        source_url: str,
        source_identity: str,
        support_type: SupportType,
        quote: str,
        quote_start_in_chunk: int,
        candidate_score: float,
        authority: SourceAuthority,
    ) -> EvidenceCandidate:
        if not isinstance(document_version, DocumentVersion):
            raise TypeError("Evidence requires a DocumentVersion")
        if not isinstance(document_chunk, DocumentChunk):
            raise TypeError("Evidence requires a DocumentChunk")
        if document_version.tenant_id != tenant_id:
            raise ValueError("Evidence tenant does not match document version")
        if document_version.document_id != document_id:
            raise ValueError("Evidence document does not match document version")
        if (
            document_version.text[
                document_chunk.start_offset : document_chunk.end_offset
            ]
            != document_chunk.text
        ):
            raise ValueError("Document chunk is not an exact span of the version")
        relative_end = quote_start_in_chunk + len(quote)
        if quote_start_in_chunk < 0 or document_chunk.text[
            quote_start_in_chunk:relative_end
        ] != quote:
            raise ValueError("Evidence quote is not at the declared chunk offset")
        start_offset = document_chunk.start_offset + quote_start_in_chunk
        end_offset = start_offset + len(quote)
        if document_version.text[start_offset:end_offset] != quote:
            raise ValueError("Evidence quote is not present in the document version")
        source = EvidenceSource(
            document_id=document_id,
            document_version_id=document_version.id,
            document_chunk_id=document_chunk_id,
            url=source_url,
            source_identity=source_identity,
            authority=authority,
        )
        return EvidenceCandidate(
            id=self._ids.new_uuid(),
            tenant_id=tenant_id,
            run_id=run_id,
            fact_requirement_id=fact_requirement_id,
            source=source,
            quote=quote,
            quote_hash=hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            support_type=support_type,
            candidate_score=candidate_score,
            start_offset=start_offset,
            end_offset=end_offset,
        )
