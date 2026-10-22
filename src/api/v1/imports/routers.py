"""Adapt administrator CSV imports, history and comparisons to the published HTTP routes."""

from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Request, Response

from src.api.v1.auth.schemas import Identity
from src.api.v1.imports.dependencies import (
    history_filters,
    import_administrator,
)
from src.api.v1.imports.schemas import (
    ImportComparisonResponse,
    ImportExecutionResponse,
    ImportFilters,
    ImportHistoryResponse,
    ImportProblemResponse,
)
from src.api.v1.imports.service import ImportExecutionService, ImportHistoryService
from src.api.v1.imports.uploads import read_upload
from src.core.infra.schemas import ErrorResponse
from src.guards.session import current_user, require_same_origin

router = APIRouter(
    route_class=DishkaRoute,
    prefix="/imports",
    tags=["Import history"],
    responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 422, 503)},
)


@router.post(
    "",
    response_model=ImportExecutionResponse,
    summary="Загрузить полный CSV-набор: только admin",
    description="Пять файлов с исходными именами, по 5 МиБ, весь multipart до 26 МиБ. Actor берётся из сессии; пути сервера не принимаются. Общий импортёр проверяет, считает и публикует полный снимок. Повтор возвращает reused без отката active. Требуется доверенный Origin.",
    dependencies=[Depends(require_same_origin)],
    responses={code: {"model": ImportProblemResponse} for code in (409, 413, 415, 422, 503)},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 5,
                                "items": {"type": "string", "format": "binary"},
                            }
                        },
                    },
                }
            },
        }
    },
)
async def upload_snapshot(
    request: Request,
    response: Response,
    user: Annotated[Identity, Depends(import_administrator)],
    service: FromDishka[ImportExecutionService],
) -> ImportExecutionResponse:
    """Authorize before parsing files and return only a settled shared-pipeline result."""
    files = await read_upload(request)
    result = await service.execute(user, files)
    response.headers["Cache-Control"] = "no-store"
    return ImportExecutionResponse(**result)


@router.get(
    "", response_model=ImportHistoryResponse, summary="История попыток импорта: только admin"
)
async def imports(
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[ImportHistoryService],
    filters: Annotated[ImportFilters, Depends(history_filters)],
) -> dict:
    """Apply role checks before returning attempts, manifest data or global counters."""
    response.headers["Cache-Control"] = "no-store"
    return await service.history(user, filters.limit, filters.offset, filters.status)


@router.get(
    "/{run_id}",
    response_model=ImportComparisonResponse,
    summary="Manifest/результат попытки и сравнение опубликованных снимков",
)
async def import_detail(
    run_id: UUID,
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[ImportHistoryService],
    base_run_id: UUID | None = None,
) -> dict:
    """Read immutable outcomes and compare payload hashes through the shared service."""
    response.headers["Cache-Control"] = "no-store"
    return await service.import_detail(user, run_id, base_run_id)
