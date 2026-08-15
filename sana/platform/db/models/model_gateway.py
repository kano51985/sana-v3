"""Tenant-scoped durable model invocation audit records."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sana.platform.db.base import Base, UUIDPrimaryKeyMixin


class ModelInvocationRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "role",
            "call_no",
            name="uq_model_invocations_attempt_role_call",
        ),
        UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_model_invocations_tenant_id_id",
        ),
        Index(
            "ix_model_invocations_tenant_run_started",
            "tenant_id",
            "run_id",
            "started_at",
        ),
        Index(
            "ix_model_invocations_logical_call",
            "tenant_id",
            "logical_call_key",
            "status",
        ),
        Index(
            "ix_model_invocations_open_attempt",
            "tenant_id",
            "attempt_id",
            "status",
        ),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("search_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("search_steps.id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("step_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    reused_from_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("model_invocations.id", ondelete="SET NULL"),
    )
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    call_no: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_call_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_called: Mapped[bool] = mapped_column(Boolean, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    output_format: Mapped[str] = mapped_column(String(32), nullable=False)
    thinking_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    input_chars: Mapped[int] = mapped_column(Integer, nullable=False)
    output_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_artifact_uri: Mapped[str | None] = mapped_column(Text)
    output_artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    provider_response_id: Mapped[str | None] = mapped_column(String(200))
    error_category: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
