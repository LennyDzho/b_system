"""
Module defining enumerations for the exceptions.
"""
from enum import Enum


class ErrorStatus(str, Enum):
    """
    Status codes for error responses.
    """
    NOT_FOUND = "NOT_FOUND"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INTERNAL = "INTERNAL"

