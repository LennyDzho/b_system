"""Share financial request filters and repository wiring between API and HTML."""

from datetime import date
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Query

from src.api.v1.auth.schemas import Identity, PropertyId
from src.api.v1.finance.schemas import DashboardResponse
from src.api.v1.finance.service import DashboardService
from src.guards.session import current_user


@inject
async def dashboard_data(
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[DashboardService],
    property_id: Annotated[
        PropertyId | None,
        Query(description="Разрешённый объект; отсутствие означает все доступные"),
    ] = None,
    date_from: Annotated[
        date | None, Query(description="Начало периода включительно; дата зависит от вида факта")
    ] = None,
    date_to: Annotated[date | None, Query(description="Конец периода включительно")] = None,
    run_id: Annotated[
        UUID | None,
        Query(description="Опубликованный снимок; по умолчанию active, прочитанный один раз"),
    ] = None,
) -> DashboardResponse:
    """Share authorization, date validation and exact financial results between API and UI."""
    return await service.dashboard(user, property_id, date_from, date_to, run_id)
