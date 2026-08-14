from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=500)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: str
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationResponse]


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    created_at: datetime
    run_id: UUID | None = None
    run_status: str | None = None
    answer_quality: str | None = None


class ConversationMessagesResponse(BaseModel):
    conversation_id: UUID
    messages: list[ConversationMessageResponse]


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class SubmissionResponse(BaseModel):
    message_id: UUID
    response_run_id: UUID
    search_run_id: UUID
    status: str
    duplicate: bool
