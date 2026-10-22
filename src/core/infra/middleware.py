"""Protect HTTP failures with an injected public error adapter and safe stream handling."""

import logging
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class SafeErrorMiddleware:
    """Prevent unhandled request failures from logging credentials or database details."""

    def __init__(
        self, app: ASGIApp, handler: Callable[[Request, Exception], Awaitable[Response]]
    ) -> None:
        """Inject HTTP translation without importing an API router into core infrastructure."""
        self.app, self.handler = app, handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle pre-response failures and sanitize errors after streaming has started."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def track_response(message: Message) -> None:
            """Track headers so a failed stream cannot cause a second HTTP response."""
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, track_response)
        except Exception as exception:
            if response_started:
                logger.error("HTTP stream failed: %s", type(exception).__name__)
                raise RuntimeError("HTTP response interrupted after headers") from None
            response = await self.handler(Request(scope), exception)
            await response(scope, receive, send)
