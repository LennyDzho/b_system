"""Wire scoped diagnostics and shared query validation for API and HTML screens."""

from datetime import date
from typing import Annotated, Literal
from uuid import UUID

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Query

from src.api.v1.auth.schemas import Identity, PropertyId
from src.api.v1.quality.schemas import QualityFilters, QualityResponse
from src.api.v1.quality.service import QualityService
from src.guards.session import current_user


def quality_filters(
    source: Annotated[str | None, Query(max_length=64)] = None,
    code: Annotated[str | None, Query(max_length=64)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    severity: Literal["info", "warning", "error"] | None = None,
    business_key: Annotated[str | None, Query(max_length=1024)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    section: Literal["all", "period", "limitations"] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> QualityFilters:
    """Validate API and HTML query parameters through one filter contract."""
    return QualityFilters(
        source=source,
        code=code,
        action=action,
        severity=severity,
        business_key=business_key,
        date_from=date_from,
        date_to=date_to,
        section=section,
        limit=limit,
        offset=offset,
    )


@inject
async def quality_data(
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[QualityService],
    filters: Annotated[QualityFilters, Depends(quality_filters)],
    property_id: Annotated[PropertyId | None, Query()] = None,
    run_id: UUID | None = None,
) -> QualityResponse:
    """Reuse server authorization and one pinned run for list, facets and counts."""
    return await service.listing(user, filters, property_id, run_id)
