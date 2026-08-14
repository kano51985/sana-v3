from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    message_id: UUID
    mode: str
    routing_reason_codes: list[str]
    route_confidence: float
    policy_version: str
    status: str
    answer_quality: str
    stop_reason: str | None
    soft_deadline_at: datetime
    hard_deadline_at: datetime
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class EvidenceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fact_key: str
    verdict: str
    confidence: float
    quote: str
    source_url: str


class MissingFactItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fact_key: str
    description: str
    status: str


class EvidenceResponse(BaseModel):
    run_id: UUID
    evidence: list[EvidenceItem]
    missing_facts: list[MissingFactItem]
