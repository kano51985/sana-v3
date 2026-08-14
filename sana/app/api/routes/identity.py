"""Authenticated identity introspection for API clients."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sana.app.api.dependencies import require_principal
from sana.modules.identity.domain import Principal


router = APIRouter(prefix="/api/v1", tags=["identity"])


class PrincipalResponse(BaseModel):
    tenant_id: UUID
    user_id: UUID
    issuer: str
    subject: str


@router.get("/me", response_model=PrincipalResponse)
async def get_me(
    principal: Principal = Depends(require_principal),
) -> PrincipalResponse:
    return PrincipalResponse(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        issuer=principal.issuer,
        subject=principal.subject,
    )
