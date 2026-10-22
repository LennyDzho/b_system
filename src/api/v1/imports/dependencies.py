"""Wire administrator import execution and audit services using shared application resources."""

from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.auth.schemas import Identity
from src.api.v1.imports.schemas import ImportFilters, ImportStatus
from src.api.v1.imports.service import require_import_admin
from src.guards.session import current_user


@inject
async def import_administrator(
    user: Annotated[Identity, Depends(current_user)],
    session: FromDishka[AsyncSession],
) -> Identity:
    """Authorize before upload, then release the read-only auth transaction for the importer."""
    require_import_admin(user)
    await session.rollback()
    return user


def history_filters(
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: ImportStatus | None = None,
) -> ImportFilters:
    """Build the common audit filter without reading any journal data."""
    return ImportFilters(limit=limit, offset=offset, status=status)
