"""
Module with custom exceptions for the application.
"""

from logging import Logger
from typing import Optional, Any


class CustomException(Exception):
    """
    Base class for custom exceptions with additional context.
    """
    detail: Optional[str]
    code: Optional[str]
    extra: dict

    def __init__(
        self, detail: Optional[str] = None, code: Optional[str] = None, **extra: Any
    ):
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
