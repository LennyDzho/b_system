"""Expose stable unversioned health URLs using the versioned infrastructure domain."""

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Response

from src.api.v1.health.schemas import LivenessResponse, ReadinessResponse
from src.api.v1.health.service import HealthService

router = APIRouter(prefix="/health", tags=["Health"], route_class=DishkaRoute)


@router.get("/live", summary="Check process liveness")
async def liveness() -> LivenessResponse:
    """Return success independently of databases and the optional broker."""
    return HealthService.liveness()


@router.get(
    "/ready",
    summary="Check both database connections",
    description="Runs concurrent database queries; returns 503 if either backend is down.",
    responses={503: {"model": ReadinessResponse, "description": "A database is unavailable"}},
)
async def readiness(response: Response, service: FromDishka[HealthService]) -> ReadinessResponse:
    """Translate real probe availability to the documented HTTP status and schema."""
    result = await service.readiness()
    response.status_code = 200 if result.status == "ready" else 503
    return result
