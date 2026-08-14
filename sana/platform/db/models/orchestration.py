"""Durable Run/Step/Attempt, event and outbox mappings."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from sana.platform.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SearchRunRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "search_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_search_runs_tenant_id_id"),
        Index("ix_search_runs_tenant_status_created", "tenant_id", "status", "created_at"),
        Index("ix_search_runs_hard_deadline", "status", "hard_deadline_at"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    response_run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("response_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    conversation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    route_reason_codes: Mapped[list[str]] = mapped_column(
        ARRAY(String(100)),
        nullable=False,
        default=list,
        server_default="{}",
    )
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    route_confidence: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="QUEUED")
    answer_quality: Mapped[str] = mapped_column(String(32), nullable=False, default="NONE")
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    soft_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    hard_deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    budget_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SearchStepRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "search_steps"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "plan_revision",
            "step_key",
            name="uq_search_steps_run_revision_key",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_search_steps_tenant_id_id"),
        Index("ix_search_steps_reconcile", "status", "retry_at", "updated_at"),
        Index("ix_search_steps_tenant_run_status", "tenant_id", "run_id", "status"),
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
    step_key: Mapped[str] = mapped_column(String(300), nullable=False)
    step_type: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="READY")
    input_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class StepAttemptRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "step_attempts"
    __table_args__ = (
        UniqueConstraint("step_id", "attempt_no", name="uq_step_attempts_step_number"),
        UniqueConstraint("idempotency_key", name="uq_step_attempts_idempotency_key"),
        UniqueConstraint("tenant_id", "id", name="uq_step_attempts_tenant_id_id"),
        Index("ix_step_attempts_lease_scan", "leased_until", "completed_at"),
        Index("ix_step_attempts_tenant_run", "tenant_id", "run_id"),
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
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(500), nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(200), nullable=False)
    leased_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(200))
    error_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    input_ref: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    output_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class OutboxEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_events_dedupe_key"),
        Index(
            "ix_outbox_events_unpublished",
            "available_at",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index("ix_outbox_events_tenant_aggregate", "tenant_id", "aggregate_id"),
    )

    tenant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(500), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class RunEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_run_events_run_sequence"),
        Index("ix_run_events_tenant_run_sequence", "tenant_id", "run_id", "sequence"),
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
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
