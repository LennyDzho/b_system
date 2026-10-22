"""Protect Dishka request isolation, resource finalization and public HTTP contracts."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from dishka import Provider, Scope
from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.helper import ClickHouseHelper
from src.api.v1.auth.service import AuthService
from src.api.v1.finance.service import DashboardService
from src.api.v1.imports.service import ImportExecutionService, ImportHistoryService
from src.api.v1.quality.service import QualityService
from src.app.__main__ import create_app
from src.app.providers import build_container
from src.core.settings import AuthConfig, load_settings
from src.database.helper import DatabaseHelper
from src.services.imports.service import ImportService


async def test_request_graph_shares_session_and_isolates_requests() -> None:
    """Cache one session per request and retain common APP helpers across request boundaries."""
    config = load_settings(env_file=None)
    container = build_container(config)
    sessions = []
    try:
        for _ in range(2):
            async with container() as request:
                session = await request.get(AsyncSession)
                sessions.append(session)
                auth = await request.get(AuthService)
                dashboard = await request.get(DashboardService)
                quality = await request.get(QualityService)
                history = await request.get(ImportHistoryService)
                execution = await request.get(ImportExecutionService)
                assert auth.repo.session is dashboard.session is quality.session is history.session
                assert auth.repo.session is session
                assert quality.dashboard is dashboard
                assert execution.pipeline is await request.get(ImportService)
                assert execution.pipeline.database is await container.get(DatabaseHelper)
                assert dashboard.analytics.helper is await container.get(ClickHouseHelper)
                assert await request.get(AuthConfig) is config.auth
                assert await request.get(AuthService) is auth
        assert sessions[0] is not sessions[1]
    finally:
        await container.close()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_http_request_closes_session_without_implicit_commit(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Finalize the real request session exactly once on response, error or cancellation."""
    application = create_app(load_settings(env_file=None))
    router = APIRouter(route_class=DishkaRoute)
    entered = asyncio.Event()
    visited: list[AsyncSession] = []
    closed: list[AsyncSession] = []
    committed: list[AsyncSession] = []
    original_close, original_commit = AsyncSession.close, AsyncSession.commit

    async def close(session: AsyncSession) -> None:
        """Observe actual session closure without replacing SQLAlchemy cleanup."""
        closed.append(session)
        await original_close(session)

    async def commit(session: AsyncSession) -> None:
        """Detect any unintended transaction commit at the request boundary."""
        committed.append(session)
        await original_commit(session)

    monkeypatch.setattr(AsyncSession, "close", close)
    monkeypatch.setattr(AsyncSession, "commit", commit)

    @router.get("/di-lifecycle")
    async def probe(session: FromDishka[AsyncSession], auth: FromDishka[AuthService]) -> dict:
        """Open a logical transaction without DB I/O, then exercise the selected HTTP exit."""
        assert session is auth.repo.session
        visited.append(session)
        await session.begin()
        entered.set()
        if outcome == "error":
            raise RuntimeError("private request context")
        if outcome == "cancel":
            await asyncio.Event().wait()
        return {"ok": True}

    application.include_router(router)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            task = asyncio.create_task(client.get("/di-lifecycle"))
            await asyncio.wait_for(entered.wait(), timeout=5)
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                response = await task
                assert response.status_code == (200 if outcome == "success" else 500)
                assert "private request context" not in response.text
    assert visited and closed == visited and not committed
    assert all(not session.in_transaction() for session in closed)


async def test_finalizer_failure_passes_through_safe_error_middleware() -> None:
    """Sanitize a failed Dishka finalizer after response headers without sending a second response."""
    override = Provider(scope=Scope.REQUEST)

    async def session() -> AsyncIterator[AsyncSession]:
        """Simulate cleanup failure after releasing a real SQLAlchemy session."""
        async with AsyncSession() as value:
            yield value
        raise RuntimeError("private finalizer context")

    override.provide(session, override=True)
    application = create_app(load_settings(env_file=None), providers=(override,))
    router = APIRouter(route_class=DishkaRoute)

    @router.get("/di-finalizer")
    async def probe(session: FromDishka[AsyncSession]) -> dict:
        """Acquire the override so its finalizer runs after the response has started."""
        return {"transaction": session.in_transaction()}

    application.include_router(router)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            with pytest.raises(
                RuntimeError, match="HTTP response interrupted after headers"
            ) as error:
                await client.get("/di-finalizer")
    assert "private finalizer context" not in str(error.value)


async def test_application_configurations_and_origin_guards_are_isolated() -> None:
    """Resolve each application's own policy without leaking config through a global container."""
    for origin in ("http://first.test", "http://second.test"):
        config = load_settings(env_file=None)
        config.auth.origin = origin
        application = create_app(config)
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url=origin
            ) as client:
                denied = await client.post(
                    "/api/v1/auth/logout", headers={"Origin": "http://foreign.test"}
                )
                no_cookie = await client.post("/api/v1/auth/logout", headers={"Origin": origin})
                assert denied.status_code == 403 and no_cookie.status_code == 401
                assert (await client.get("/login")).status_code == 200
                assert (await client.get("/health/live")).status_code == 200


def test_openapi_hides_container_dependencies_and_keeps_session_security() -> None:
    """Keep FromDishka services and sessions out of HTTP parameters and preserve the cookie scheme."""
    schema = create_app(load_settings(env_file=None)).openapi()
    assert schema["components"]["securitySchemes"]["ServerSession"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "villa_session",
    }
    for path in schema["paths"].values():
        for operation in path.values():
            names = {parameter["name"] for parameter in operation.get("parameters", [])}
            assert not names & {"service", "session", "config", "imports", "analytics"}
            assert not any("dishka" in name for name in names)
    assert schema["paths"]["/api/v1/dashboard"]["get"]["security"] == [{"ServerSession": []}]
