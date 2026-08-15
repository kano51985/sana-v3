from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from sana.app.api.dependencies import get_container, require_principal
from sana.app.api.schemas.conversations import (
    ConversationCreate,
    ConversationListResponse,
    ConversationMessagesResponse,
    ConversationResponse,
    MessageCreate,
    SubmissionResponse,
)
from sana.modules.conversation.domain import SubmitMessageCommand
from sana.modules.identity.domain import Principal
from sana.modules.shared.ids import TraceContext


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _catalog(request: Request):
    catalog = get_container(request).conversation_catalog
    if catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Conversation catalog is unavailable",
        )
    return catalog


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
    ),
) -> ConversationResponse:
    conversation = await _catalog(request).create(
        principal,
        body.title,
        idempotency_key,
    )
    return ConversationResponse.model_validate(conversation)


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> ConversationListResponse:
    items = await _catalog(request).list(principal)
    return ConversationListResponse(conversations=items)


@router.get("/{conversation_id}/messages", response_model=ConversationMessagesResponse)
async def list_messages(
    conversation_id: UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> ConversationMessagesResponse:
    messages = await _catalog(request).messages(principal, conversation_id)
    if messages is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation not found",
        )
    return ConversationMessagesResponse(
        conversation_id=conversation_id,
        messages=messages,
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=SubmissionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_message(
    conversation_id: UUID,
    body: MessageCreate,
    request: Request,
    principal: Principal = Depends(require_principal),
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
    ),
) -> SubmissionResponse:
    container = get_container(request)
    routing = container.router.route(body.content)
    receipt = await container.conversation_service.submit_message(
        SubmitMessageCommand(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=conversation_id,
            content=body.content,
            idempotency_key=idempotency_key,
            routing=routing,
            trace_context=TraceContext.create(),
        )
    )
    return SubmissionResponse.model_validate(receipt, from_attributes=True)
