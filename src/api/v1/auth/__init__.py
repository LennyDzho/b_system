"""Export auth contracts and use cases without importing HTTP routers."""

from src.api.v1.auth.schemas import (
    AccessScope,
    CreateUserData,
    CurrentUserResponse,
    Identity,
    LoginInput,
    MessageResponse,
    PropertyId,
    Role,
    ScopeResponse,
    Username,
)
from src.api.v1.auth.service import AuthService

__all__ = [
    "AccessScope",
    "CreateUserData",
    "CurrentUserResponse",
    "Identity",
    "LoginInput",
    "MessageResponse",
    "PropertyId",
    "Role",
    "ScopeResponse",
    "Username",
    "AuthService",
]
