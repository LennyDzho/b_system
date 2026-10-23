"""Protect financial drill-down selection, safe return context and native HTML filters."""

from datetime import date
from typing import Literal

from dishka import make_async_container
from dishka.integrations.fastapi import setup_dishka
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from web.routing import HtmlRoute


def test_native_blank_html_controls_keep_shared_validation_and_api_contracts() -> None:
    """Support no-JS GET forms without accepting malformed nonempty inputs or changing API rules."""
    application = FastAPI()
    setup_dishka(make_async_container(), application)
    router = APIRouter(route_class=HtmlRoute)

    @router.get("/html")
    async def html(
        source: Literal["expenses.csv"] | None = None, date_from: date | None = None
    ) -> dict[str, str | None]:
        """Use ordinary typed filter parameters, just like the real HTML domain routes."""
        return {"source": source, "date_from": date_from.isoformat() if date_from else None}

    @application.get("/api")
    async def api(date_from: date | None = None) -> dict[str, str | None]:
        """Keep API validation separate from browser form normalization."""
        return {"date_from": date_from.isoformat() if date_from else None}

    application.include_router(router)
    with TestClient(application) as client:
        assert client.get("/html?source=&date_from=").json() == {"source": None, "date_from": None}
        assert client.get("/html?source=expenses.csv&date_from=2026-09-01").status_code == 200
        assert client.get("/html?date_from=bad").status_code == 422
        assert client.get("/html?source=unknown").status_code == 422
        assert client.get("/api?date_from=").status_code == 422
