"""
Authentication guard for API key validation.
"""
from fastapi import Security

from fastapi.security.api_key import APIKey, APIKeyHeader

from src.core.infra.exceptions import NotApiKey, InvalidApiKey
from core.settings import settings


api_key_header = APIKeyHeader(
    name="x-api-key",
    auto_error=False,
    scheme_name="x-api-key",
    description="Авторизация по API key",
)


async def auth_guard(api_key: APIKey = Security(api_key_header)) -> APIKey:
    """
    Authentication guard that validates the API key from the request header.
    """

    if not api_key:
        raise NotApiKey(detail="Api key not found")

    if api_key != settings.app.api_key:
        raise InvalidApiKey(detail="Invalid api key")
    return api_key
