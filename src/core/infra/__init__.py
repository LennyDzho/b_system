"""
Core infrastructure module for error handling and responses.
"""

__all__ = ["ErrorStatus", "ErrorJsonResponse"]

from .enums import ErrorStatus
from .responses import ErrorJsonResponse
