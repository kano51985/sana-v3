"""Search planning, retrieval, evidence and citation mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sana.platform.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FactRequirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "fact_requirements"
    __table_args__ = (
        UniqueConstraint("run_id", "fact_key", name="uq_fact_requirements_run_key"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "id",
            name="uq_fact_requirements_tenant_run_id",
        ),
        Index("ix_fact_requirements_tenant_run_status", "tenant_id", "run_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    fact_key: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    freshness: Mapped[str] = mapped_column(String(32), nullable=False, default="STABLE")
    consequence: Mapped[str] = mapped_column(String(32), nullable=False, default="LOW")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")


class QuerySpec(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "query_specs"
    __table_args__ = (
        UniqueConstraint("run_id", "plan_revision", "query_key", name="uq_query_specs_run_revision_key"),
        Index("ix_query_specs_tenant_run_revision", "tenant_id", "run_id", "plan_revision"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    fact_requirement_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("fact_requirements.id", ondelete="SET NULL"))
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    query_key: Mapped[str] = mapped_column(String(200), nullable=False)
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    provider_class: Mapped[str] = mapped_column(String(100), nullable=False)
    locale: Mapped[str | None] = mapped_column(String(32))
    freshness_days: Mapped[int | None] = mapped_column(Integer)
    query_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class ProviderAttempt(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "provider_attempts"
    __table_args__ = (
        UniqueConstraint(
            "query_spec_id",
            "provider",
            "attempt_no",
            name="uq_provider_attempts_query_provider_number",
        ),
        Index("ix_provider_attempts_tenant_run_started", "tenant_id", "run_id", "started_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    query_spec_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("query_specs.id", ondelete="CASCADE"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(200))


class SearchHit(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "search_hits"
    __table_args__ = (
        UniqueConstraint("provider_attempt_id", "url_hash", name="uq_search_hits_attempt_url"),
        Index("ix_search_hits_tenant_run_score", "tenant_id", "run_id", "score"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    provider_attempt_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("provider_attempts.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FetchArtifact(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "fetch_artifacts"
    __table_args__ = (
        UniqueConstraint("run_id", "url_hash", "attempt_no", name="uq_fetch_artifacts_run_url_attempt"),
        Index("ix_fetch_artifacts_tenant_run_status", "tenant_id", "run_id", "status"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    search_hit_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_hits.id", ondelete="SET NULL"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    fetcher: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    media_type: Mapped[str | None] = mapped_column(String(200))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    storage_uri: Mapped[str | None] = mapped_column(Text)
    response_bytes: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetch_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "canonical_url_hash", name="uq_documents_tenant_url"),
        UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id_id"),
        Index("ix_documents_tenant_updated", "tenant_id", "updated_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_host: Mapped[str] = mapped_column(String(500), nullable=False)


class DocumentVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "content_hash", name="uq_document_versions_document_hash"),
        UniqueConstraint("tenant_id", "id", name="uq_document_versions_tenant_id_id"),
        Index("ix_document_versions_tenant_document_fetched", "tenant_id", "document_id", "fetched_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    fetch_artifact_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("fetch_artifacts.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    language: Mapped[str | None] = mapped_column(String(32))
    text_length: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    document_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")


class DocumentChunk(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "ordinal", name="uq_document_chunks_version_ordinal"),
        UniqueConstraint("tenant_id", "id", name="uq_document_chunks_tenant_id_id"),
        Index("ix_document_chunks_tenant_version", "tenant_id", "document_version_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)


class EvidenceCandidate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "evidence_candidates"
    __table_args__ = (
        UniqueConstraint("fact_requirement_id", "document_chunk_id", "quote_hash", name="uq_evidence_candidates_fact_chunk_quote"),
        UniqueConstraint("tenant_id", "id", name="uq_evidence_candidates_tenant_id_id"),
        Index("ix_evidence_candidates_tenant_run_fact", "tenant_id", "run_id", "fact_requirement_id"),
        CheckConstraint(
            "start_offset >= 0 AND end_offset >= start_offset",
            name="quote_offsets",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    fact_requirement_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("fact_requirements.id", ondelete="CASCADE"), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False)
    document_chunk_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    support_type: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_score: Mapped[float] = mapped_column(Float, nullable=False)
    source_identity: Mapped[str] = mapped_column(String(500), nullable=False)
    source_authority: Mapped[str] = mapped_column(String(32), nullable=False)


class VerifiedEvidence(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "verified_evidence"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_verified_evidence_candidate"),
        UniqueConstraint("tenant_id", "id", name="uq_verified_evidence_tenant_id_id"),
        Index("ix_verified_evidence_tenant_run_verdict", "tenant_id", "run_id", "verdict"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("evidence_candidates.id", ondelete="CASCADE"), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    verifier_version: Mapped[str] = mapped_column(String(100), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AnswerClaim(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "answer_claims"
    __table_args__ = (
        UniqueConstraint("run_id", "claim_key", name="uq_answer_claims_run_key"),
        UniqueConstraint("tenant_id", "id", name="uq_answer_claims_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id", "fact_requirement_id"],
            [
                "fact_requirements.tenant_id",
                "fact_requirements.run_id",
                "fact_requirements.id",
            ],
            name="fk_answer_claims_tenant_run_fact",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "(claim_kind IS NULL OR claim_kind IN "
            "('FACTUAL', 'UNCERTAINTY', 'COMMENTARY')) AND "
            "(claim_kind IS DISTINCT FROM 'FACTUAL' OR fact_requirement_id IS NOT NULL)",
            name="kind_fact_binding",
        ),
        Index("ix_answer_claims_tenant_run", "tenant_id", "run_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    claim_key: Mapped[str] = mapped_column(String(200), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    support_status: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_kind: Mapped[str | None] = mapped_column(String(32))
    fact_requirement_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True))


class Citation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "citations"
    __table_args__ = (
        UniqueConstraint("answer_claim_id", "ordinal", name="uq_citations_claim_ordinal"),
        Index("ix_citations_tenant_run", "tenant_id", "run_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("search_runs.id", ondelete="CASCADE"), nullable=False)
    answer_claim_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("answer_claims.id", ondelete="CASCADE"), nullable=False)
    verified_evidence_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("verified_evidence.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    rendered_url: Mapped[str] = mapped_column(Text, nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_chunk_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False)
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False)
