"""Normalize empty native HTML filters before the shared typed request validation."""

from collections.abc import Awaitable, Callable

from dishka.integrations.fastapi import DishkaRoute
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import Response


class HtmlRoute(DishkaRoute):
    """Keep Dishka injection and support native GET forms without requiring JavaScript."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        """Treat blank HTML form controls as absent while retaining all nonempty validation."""
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            """Normalize a copied HTTP scope; API routes continue using their original contracts."""
            values = request.query_params.multi_items()
            nonempty = [(key, value) for key, value in values if value != ""]
            if len(nonempty) != len(values):
                scope = {
                    **request.scope,
                    "query_string": str(QueryParams(nonempty)).encode("ascii"),
                }
                request = Request(scope, request.receive)
            return await handler(request)

        return handle
