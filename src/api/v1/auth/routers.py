"""Expose thin login/logout and current-user HTTP adapters for server sessions."""

from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Request, Response

from src.api.v1.auth.schemas import (
    AccessScope,
    CurrentUserResponse,
    Identity,
    LoginInput,
    MessageResponse,
    ScopeResponse,
)
from src.api.v1.auth.service import AuthService
from src.core.infra.schemas import ErrorResponse
from src.core.settings import AuthConfig
from src.guards.session import access_scope, current_user, require_same_origin

router = APIRouter(prefix="/auth", tags=["Authentication"], route_class=DishkaRoute)
ERRORS = {
    401: {"model": ErrorResponse, "description": "Missing, expired, or invalid credentials"},
    403: {"model": ErrorResponse, "description": "Origin or property access denied"},
    404: {"model": ErrorResponse, "description": "Property absent from a published dictionary"},
    422: {"model": ErrorResponse, "description": "Invalid input without echoing secrets"},
}


@router.post(
    "/login",
    summary="Create a server session",
    responses=ERRORS,
    description="Requires Origin equal to AUTH_ORIGIN. Sets an HttpOnly cookie; no token in JSON.",
    dependencies=[Depends(require_same_origin)],
)
async def login(
    credentials: LoginInput,
    request: Request,
    response: Response,
    service: FromDishka[AuthService],
    config: FromDishka[AuthConfig],
) -> MessageResponse:
    """Set the cookie only after the server-side session transaction commits."""
    token = await service.login(
        credentials.username,
        credentials.password.get_secret_value(),
        request.cookies.get(config.cookie_name),
    )
    response.set_cookie(
        config.cookie_name,
        token,
        max_age=config.session_seconds,
        httponly=True,
        secure=config.secure_cookie,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return MessageResponse(message="Вход выполнен")


@router.post(
    "/logout",
    summary="Revoke the current server session",
    responses=ERRORS,
    description="Requires the session cookie and the exact trusted Origin.",
    dependencies=[Depends(require_same_origin)],
)
async def logout(
    request: Request,
    response: Response,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[AuthService],
    config: FromDishka[AuthConfig],
) -> MessageResponse:
    """Revoke the token in PostgreSQL before clearing its browser cookie."""
    await service.logout(request.cookies[config.cookie_name])
    response.delete_cookie(
        config.cookie_name, httponly=True, secure=config.secure_cookie, samesite="lax", path="/"
    )
    response.headers["Cache-Control"] = "no-store"
    return MessageResponse(message="Выход выполнен")


@router.get(
    "/me",
    summary="Read the current user and authorized property scope",
    responses=ERRORS,
    description="Checks session and current rights. An optional foreign property_id returns 403.",
)
async def me(
    response: Response, scope: Annotated[AccessScope, Depends(access_scope)]
) -> CurrentUserResponse:
    """Return explicit access semantics using the common access dependency."""
    response.headers["Cache-Control"] = "no-store"
    return CurrentUserResponse(
        id=scope.user.id,
        username=scope.user.username,
        role=scope.user.role,
        access=ScopeResponse(
            all_properties=scope.property_ids is None,
            property_ids=list(scope.property_ids or ()),
        ),
    )
