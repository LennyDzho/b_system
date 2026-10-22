"""Render diagnostic and administrative screens using the common authorized services."""

from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.api.v1.auth.schemas import Identity
from src.api.v1.quality.dependencies import quality_data
from src.api.v1.quality.schemas import QualityResponse
from src.api.v1.quality.service import QualityService
from src.guards.session import current_user
from web.templating import templates

router = APIRouter(include_in_schema=False, route_class=DishkaRoute)


@router.get("/quality")
async def quality_page(
    request: Request,
    data: Annotated[QualityResponse, Depends(quality_data)],
    user: Annotated[Identity, Depends(current_user)],
) -> HTMLResponse:
    """Render scoped filters, separate counts, period issues and snapshot limitations."""
    return templates.TemplateResponse(
        request, "quality.html", {"data": data, "user": user}, headers={"Cache-Control": "no-store"}
    )


@router.get("/quality/issues/{issue_id}")
async def issue_page(
    request: Request,
    issue_id: str,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[QualityService],
    run_id: UUID | None = None,
) -> HTMLResponse:
    """Explain original facts and related entities after server authorization."""
    data = await service.detail(user, issue_id, run_id)
    return templates.TemplateResponse(
        request,
        "quality_detail.html",
        {"data": data, "user": user},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/quality/raw/{source}/{row_number}")
async def raw_page(
    request: Request,
    source: str,
    row_number: int,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[QualityService],
    run_id: UUID | None = None,
) -> HTMLResponse:
    """Show exact raw rows even when their business identifier is invalid or absent."""
    data = await service.raw_detail(user, source, row_number, run_id)
    return templates.TemplateResponse(
        request,
        "quality_raw.html",
        {"data": data, "user": user},
        headers={"Cache-Control": "no-store"},
    )
