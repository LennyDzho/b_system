"""Render administrator import history using the dedicated import-history domain."""

from typing import Annotated, Literal
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from src.api.v1.auth.schemas import Identity
from src.api.v1.imports.service import ImportHistoryService
from src.guards.session import current_user
from src.services.imports.records import SCHEMAS
from web.templating import templates

router = APIRouter(include_in_schema=False, route_class=DishkaRoute)


@router.get("/imports")
async def history_page(
    request: Request,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[ImportHistoryService],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: Literal["running", "failed", "published"] | None = None,
) -> HTMLResponse:
    """Render immutable attempts and active identity only for administrators."""
    data = await service.history(user, limit, offset, status)
    return templates.TemplateResponse(
        request,
        "imports.html",
        {"data": data, "user": user, "import_files": sorted(SCHEMAS)},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/imports/{run_id}")
async def import_page(
    request: Request,
    run_id: UUID,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[ImportHistoryService],
    base_run_id: UUID | None = None,
) -> HTMLResponse:
    """Render manifest, persisted outcome and an explicit comparison baseline."""
    data = await service.import_detail(user, run_id, base_run_id)
    return templates.TemplateResponse(
        request,
        "import_detail.html",
        {"data": data, "user": user},
        headers={"Cache-Control": "no-store"},
    )
