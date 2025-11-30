from logging import Logger
from typing import Optional, Any


class CustomException(Exception):

    detail: Optional[str]
    code: Optional[str]
    extra: dict

    def __init__(
        self, detail: Optional[str] = None, code: Optional[str] = None, **extra: Any
    ):
        self.detail = detail
        self.code = code
        self.extra = extra
        super().__init__(detail)

    def __str__(self) -> str:
        code_part = f" [{self.code}]" if self.code else ""
        extra_part = f" | {self.extra}" if self.extra else ""
        return f"{self.__class__.__name__}: {self.detail or ''}{code_part}{extra_part}"

    def log(self, logger: Logger) -> None:
        logger.error(str(self))


class NotFound(CustomException):
    """The record was not found"""


class NotApiKey(CustomException):
    """API key is not specified"""


class InvalidApiKey(CustomException):
    """Invalid API key"""
