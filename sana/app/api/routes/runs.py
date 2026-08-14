from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from sana.app.api.dependencies import get_container, require_principal
from sana.app.api.schemas.runs import EvidenceResponse, RunResponse
from sana.modules.identity.domain import Principal


router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> RunResponse:
    view = await get_container(request).run_service.get(principal, run_id)
    if view is None:
        raise _not_found()
    return RunResponse.model_validate(view)


@router.post("/{run_id}/cancel", response_model=RunResponse)
async def cancel_run(
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> RunResponse:
    view = await get_container(request).run_service.cancel(principal, run_id)
    if view is None:
        raise _not_found()
    return RunResponse.model_validate(view)


@router.get("/{run_id}/evidence", response_model=EvidenceResponse)
async def get_evidence(
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> EvidenceResponse:
    report = await get_container(request).run_service.evidence(principal, run_id)
    if report is None:
        raise _not_found()
    return EvidenceResponse(
        run_id=run_id,
        evidence=list(report.evidence),
        missing_facts=list(report.missing_facts),
    )
