"""Expose thin authenticated financial endpoints with one snapshot-bound service."""

from typing import Annotated, Any
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Response

from src.api.v1.auth.schemas import Identity
from src.api.v1.finance.dependencies import dashboard_data
from src.api.v1.finance.schemas import DashboardResponse
from src.api.v1.finance.service import DashboardService
from src.core.infra.schemas import ErrorResponse
from src.guards.session import current_user

router = APIRouter(
    route_class=DishkaRoute,
    tags=["Finance"],
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    summary="Финансы портфеля IDR",
    description="Денежные значения — decimal-строки. Owner estimate — оценка до неизвестных комиссий. Empty, no_snapshot и not_calculated различаются; отказ БД — HTTP 503.",
)
async def dashboard(
    response: Response, data: Annotated[DashboardResponse, Depends(dashboard_data)]
) -> DashboardResponse:
    """Return precomputed source decisions and Decimal totals without browser-side formulas."""
    response.headers["Cache-Control"] = "no-store"
    return data


@router.get("/properties", summary="Доступный справочник объектов")
async def properties(
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[DashboardService],
) -> list[dict[str, Any]]:
    """Expose only the currently authorized published property dictionary."""
    data = await service.dashboard(user)
    response.headers["Cache-Control"] = "no-store"
    return [
        {"property_id": row["property_id"], "property_name": row["property_name"]}
        for row in data.properties
    ]


@router.get(
    "/financial-records/{source}/{key}",
    summary="Финансовое решение, исходные строки и формулы",
    description="source: reservations, expenses или payments. Доступ проверяется заново по опубликованному run_id. Возвращаются decimal-строки, raw, canonical, issues, правила и связанные платежи.",
)
async def detail(
    source: str,
    key: str,
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[DashboardService],
    run_id: UUID | None = None,
) -> dict[str, Any]:
    """Read provenance only after checking current server rights on the same snapshot."""
    data = await service.detail(user, source + ".csv", key, run_id)
    response.headers["Cache-Control"] = "no-store"
    return data


@router.get("/reservations/{reservation_id}", summary="Детализация брони и её cash")
async def reservation(
    reservation_id: str,
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[DashboardService],
    run_id: UUID | None = None,
) -> dict[str, Any]:
    """Offer the domain-specific reservation contract using the common detail implementation."""
    return await detail("reservations", reservation_id, response, user, service, run_id)
