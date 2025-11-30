"""
Schemas for API responses and error handling.
"""
from pydantic import BaseModel, Field

from src.core.infra.enums import ErrorStatus


class BaseApiResponse(BaseModel):
    """
    Base schema for API responses.
    """
    status: str = Field(default="ok", examples=["ok", "error"])
    message: str


class ErrorData(BaseModel):
    """
    Schema for error details in error responses.
    """
    code: int
    message: str
    status: ErrorStatus


class ErrorResponse(BaseModel):
    """
    Schema for error responses.
    """
    error: ErrorData
