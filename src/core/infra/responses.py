"""
Module defining custom JSON responses for error handling.
"""

from fastapi.responses import JSONResponse

from src.core.infra.enums import ErrorStatus
from src.core.infra.schemas import ErrorData, ErrorResponse


class ErrorJsonResponse(JSONResponse):
    """
    Custom JSON response for error handling.
    """

    def __init__(
        self,
        *,
        code: int,
        message: str,
        status: ErrorStatus,
    ) -> None:
        """Serialize a status, message, and code using the shared error schema."""
        error_data = ErrorData(code=code, message=message, status=status)
        super().__init__(
            status_code=code,
            content=ErrorResponse(error=error_data).model_dump(),
            media_type="application/json; charset=utf-8",
        )
