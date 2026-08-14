from uuid import UUID

from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class SubmissionResponse(BaseModel):
    message_id: UUID
    response_run_id: UUID
    search_run_id: UUID
    status: str
    duplicate: bool
