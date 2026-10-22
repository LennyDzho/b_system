"""Translate core failures to one safe JSON or HTML HTTP contract."""

import logging

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from src.core.infra.enums import ErrorStatus
from src.core.infra.exceptions import (
    AuthError,
    DashboardError,
    InvalidApiKey,
    NotApiKey,
    NotFound,
    WebImportError,
)
from src.core.infra.responses import ErrorJsonResponse

logger = logging.getLogger(__name__)


def is_web_path(path: str) -> bool:
    """Identify protected screens independently of domain router imports."""
    return path in {"/account", "/dashboard", "/quality", "/imports"} or path.startswith(
        ("/detail/", "/quality/", "/imports/")
    )


def is_data_path(path: str) -> bool:
    """Recognize analytical entry points where backend failures mean unavailable data."""
    return path in {
        "/dashboard",
        "/api/v1/dashboard",
        "/api/v1/properties",
        "/quality",
        "/imports",
        "/api/v1/quality-issues",
        "/api/v1/imports",
    } or path.startswith(
        (
            "/detail/",
            "/api/v1/reservations/",
            "/api/v1/financial-records/",
            "/quality/",
            "/imports/",
            "/api/v1/quality-issues/",
            "/api/v1/source-rows/",
            "/api/v1/imports/",
        )
    )


def error_response(code: int, message: str, status: ErrorStatus) -> ErrorJsonResponse:
    """Keep every public failure in the common envelope without browser caching."""
    response = ErrorJsonResponse(code=code, message=message, status=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def import_problem_response(exception: WebImportError) -> JSONResponse:
    """Keep upload and pre-upload backend failures in the documented import envelope."""
    return JSONResponse(
        status_code=exception.status_code,
        headers={"Cache-Control": "no-store"},
        content={
            "error": {
                "code": exception.status_code,
                "message": exception.detail,
                "status": ErrorStatus.INVALID_ARGUMENT
                if exception.status_code < 500
                else ErrorStatus.INTERNAL,
            },
            "outcome": exception.outcome,
            "run_id": str(exception.run_id) if exception.run_id else None,
        },
    )


async def custom_exception_handler(request: Request, exception: Exception) -> Response:
    """Handle core exceptions centrally, hiding request secrets and database details.

    Expected access errors carry deliberately public messages. Validation and unexpected
    errors never echo input or exception text. A failed backend is distinct from zero
    data; protected HTML redirects only an unauthenticated user to the login screen.
    """
    if isinstance(exception, WebImportError):
        return import_problem_response(exception)
    if isinstance(exception, AuthError):
        if is_web_path(request.url.path) and exception.status_code == 401:
            return RedirectResponse(
                "/login", status_code=303, headers={"Cache-Control": "no-store"}
            )
        status = {
            401: ErrorStatus.UNAUTHENTICATED,
            404: ErrorStatus.NOT_FOUND,
            422: ErrorStatus.INVALID_ARGUMENT,
        }.get(exception.status_code, ErrorStatus.PERMISSION_DENIED)
        return error_response(exception.status_code, exception.detail or "Ошибка доступа", status)
    if isinstance(exception, NotApiKey):
        return error_response(401, "API key is required", ErrorStatus.UNAUTHENTICATED)
    if isinstance(exception, InvalidApiKey):
        return error_response(403, "Invalid API key", ErrorStatus.PERMISSION_DENIED)
    if isinstance(exception, NotFound):
        return error_response(404, "Запись не найдена", ErrorStatus.NOT_FOUND)
    if isinstance(exception, RequestValidationError):
        return error_response(422, "Некорректные параметры запроса", ErrorStatus.INVALID_ARGUMENT)
    unavailable = isinstance(exception, DashboardError) or (
        is_data_path(request.url.path)
        and isinstance(exception, (SQLAlchemyError, OSError, TimeoutError))
    )
    if unavailable:
        if request.method == "POST" and request.url.path == "/api/v1/imports":
            logger.error("Import backend unavailable: %s", type(exception).__name__)
            return import_problem_response(
                WebImportError(
                    503, "База данных недоступна. Импорт не запущен.", outcome="unavailable"
                )
            )
        message = str(exception) if isinstance(exception, DashboardError) else str(DashboardError())
        if not isinstance(exception, DashboardError):
            logger.error("Backend unavailable: %s", type(exception).__name__)
        if is_web_path(request.url.path):
            from web.templating import templates

            return templates.TemplateResponse(
                request,
                "finance_error.html",
                {
                    "message": message,
                    "title": "Данные недоступны"
                    if request.url.path.startswith(("/quality", "/imports"))
                    else "Финансовые данные недоступны",
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return error_response(503, message, ErrorStatus.INTERNAL)
    logger.error("Unhandled application exception: %s", type(exception).__name__)
    return error_response(500, "Internal server error", ErrorStatus.INTERNAL)
