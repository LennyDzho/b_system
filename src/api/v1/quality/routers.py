"""Expose authenticated diagnostic filters and source provenance."""

from typing import Annotated, Any
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Response

from src.api.v1.auth.schemas import Identity
from src.api.v1.quality.dependencies import quality_data
from src.api.v1.quality.schemas import QualityResponse
from src.api.v1.quality.service import QualityService
from src.core.infra.schemas import ErrorResponse
from src.guards.session import current_user

router = APIRouter(
    route_class=DishkaRoute,
    tags=["Data quality"],
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)},
)


@router.get(
    "/quality-issues",
    response_model=QualityResponse,
    summary="Качество данных: проблемы, отдельные счётчики и пагинация",
    description="Фильтры source/code/severity/action/business_key/property_id/date_from/date_to, section=all|period|limitations, limit/offset. Даты включительны. Глобальные/недатированные ограничения не скрываются периодом. Scope применяется до списков, связей и счётчиков.",
)
async def issues(
    response: Response, data: Annotated[QualityResponse, Depends(quality_data)]
) -> QualityResponse:
    """Return the same permission-filtered issues rendered by Jinja."""
    response.headers["Cache-Control"] = "no-store"
    return data


@router.get(
    "/quality-issues/{issue_id}",
    summary="Проблема, связанные записи, исходные строки и влияние на суммы",
)
async def issue_detail(
    issue_id: str,
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[QualityService],
    run_id: UUID | None = None,
) -> dict[str, Any]:
    """Check fresh server rights before exposing original or related source values."""
    response.headers["Cache-Control"] = "no-store"
    return await service.detail(user, issue_id, run_id)


@router.get(
    "/source-rows/{source}/{row_number}",
    summary="Исходная строка, включая справочники и проблемные ID",
)
async def source_row(
    source: str,
    row_number: int,
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[QualityService],
    run_id: UUID | None = None,
) -> dict[str, Any]:
    """Expose exact raw lineage only for permitted original objects in published snapshots."""
    response.headers["Cache-Control"] = "no-store"
    return await service.raw_detail(user, source, row_number, run_id)
