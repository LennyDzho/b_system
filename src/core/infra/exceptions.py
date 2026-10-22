"""
Module with custom exceptions for the application.
"""

from logging import Logger
from typing import Any, Optional
from uuid import UUID


class CustomException(Exception):
    """
    Base class for custom exceptions with additional context.
    """

    detail: Optional[str]
    code: Optional[str]
    extra: dict[str, Any]

    def __init__(
        self, detail: Optional[str] = None, code: Optional[str] = None, **extra: Any
    ) -> None:
        """
        Initializes the CustomException.
        """
        self.detail = detail
        self.code = code
        self.extra = extra
        super().__init__(detail)

    def __str__(self) -> str:
        """
        String representation of the exception.
        """
        code_part = f" [{self.code}]" if self.code else ""
        extra_part = f" | {self.extra}" if self.extra else ""
        return f"{self.__class__.__name__}: {self.detail or ''}{code_part}{extra_part}"

    def log(self, logger: Logger) -> None:
        """
        Logs the exception using the provided logger.
        """
        logger.error(str(self))


class NotFound(CustomException):
    """The record was not found"""


class NotApiKey(CustomException):
    """API key is not specified"""


class InvalidApiKey(CustomException):
    """Invalid API key"""


class AuthError(CustomException):
    """Represent an expected public authentication/authorization failure."""

    def __init__(self, status_code: int, detail: str) -> None:
        """Carry only a deliberately public message and its HTTP status."""
        super().__init__(detail=detail)
        self.status_code = status_code


class DashboardError(Exception):
    """Carry an intentionally public availability error without database details."""

    def __init__(
        self, message: str = "База данных недоступна. Финансовые показатели не получены."
    ) -> None:
        """Describe failure separately from an empty financial selection."""
        super().__init__(message)


class ImportFailed(RuntimeError):
    """A definite failed attempt, with a public nonsecret reason and optional ID."""

    def __init__(
        self, message: str, *, run_id: UUID | None = None, code: str = "import_failed"
    ) -> None:
        """Preserve CLI messages while carrying typed attempt metadata to other transports."""
        super().__init__(message)
        self.run_id, self.code = run_id, code


class PublicationUnknown(RuntimeError):
    """Commit may have succeeded; do not label the attempt failed without reconciliation."""

    def __init__(self, message: str, *, run_id: UUID | None = None) -> None:
        """Identify the uncertain attempt without asking callers to parse exception messages."""
        super().__init__(message)
        self.run_id = run_id


class ImportBusy(RuntimeError):
    """Another connection already owns the single importer lease."""


class ImportLockLost(RuntimeError):
    """The original PostgreSQL backend or advisory lease disappeared."""


class SnapshotMismatch(RuntimeError):
    """Reject an unverified or duplicated physical snapshot before publication."""


class WebImportError(CustomException):
    """Carry a safe upload/import outcome to the common HTTP exception adapter."""

    def __init__(
        self, status_code: int, detail: str, *, outcome: str = "invalid", run_id: UUID | None = None
    ) -> None:
        """Expose only an intentional public message and a verified attempt identifier."""
        super().__init__(detail=detail)
        self.status_code, self.outcome, self.run_id = status_code, outcome, run_id
