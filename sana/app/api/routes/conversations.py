from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from sana.app.api.dependencies import get_container, require_principal
from sana.app.api.schemas.conversations import MessageCreate, SubmissionResponse
from sana.modules.conversation.domain import SubmitMessageCommand
from sana.modules.identity.domain import Principal
from sana.modules.shared.ids import TraceContext


router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


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
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
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
