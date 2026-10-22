"""Enforce server sessions, current property scopes, and browser origin checks."""

from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, Query, Request, Security
from fastapi.security import APIKeyCookie

from src.analytics.imports import AnalyticsImportRepo
from src.api.v1.auth.schemas import AccessScope, Identity
from src.api.v1.auth.service import AuthService, scope_for
from src.core.infra.exceptions import AuthError
from src.core.settings import AuthConfig
from src.database.repositories.imports import ImportRepo

session_cookie = APIKeyCookie(name="villa_session", auto_error=False, scheme_name="ServerSession")


@inject
async def current_user(
    token: Annotated[str | None, Security(session_cookie)],
    service: FromDishka[AuthService],
) -> Identity:
    """Require a nonexpired active server session; no API key can replace it."""
    return await service.identity(token)


@inject
async def access_scope(
    user: Annotated[Identity, Depends(current_user)],
    imports: FromDishka[ImportRepo],
    analytics: FromDishka[AnalyticsImportRepo],
    property_id: Annotated[
        str | None,
        Query(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Optional external property ID"),
    ] = None,
) -> AccessScope:
    """Constrain an optional object filter using the user's current server rights."""
    scope = scope_for(user, property_id)
    active = await imports.active_id()
    if active is None:
        return scope
    known = await analytics.property_ids(active)
    if property_id is not None and property_id not in known:
        raise AuthError(404, "Объект отсутствует в опубликованном справочнике")
    if scope.property_ids is not None:
        return AccessScope(user, tuple(key for key in scope.property_ids if key in known))
    return scope


@inject
async def require_same_origin(request: Request, config: FromDishka[AuthConfig]) -> None:
    """Reject missing, null, or foreign Origin on all current state-changing routes."""
    if request.headers.get("origin") != config.origin:
        raise AuthError(403, "Для этого запроса требуется доверенный Origin")
