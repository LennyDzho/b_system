"""Test HTTP contracts, finite probes, configuration, and resource cleanup."""

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dishka.exceptions import ExitError
from fastapi import Request
from fastapi.testclient import TestClient

from src.analytics.helper import ClickHouseHelper
from src.api.v1.health.service import check_connection
from src.app.__main__ import create_app
from src.core.infra.exceptions import InvalidApiKey, NotApiKey
from src.core.settings import BASE_DIR, load_settings
from src.database.helper import DatabaseHelper


def test_settings_are_independent_of_cwd_and_optional_services(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Resolve input paths from the root without requiring AMQP or Redis."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", "data/nested")
    monkeypatch.setenv("BROKER_ENABLED", "False")
    config = load_settings(env_file=None)
    assert config.app.data_dir == BASE_DIR / "data" / "nested"
    assert not config.rabbitmq.enabled
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert load_settings(env_file=None).app.data_dir == tmp_path


def test_database_url_preserves_special_password_characters() -> None:
    """Build a typed URL whose secret never appears in its normal rendering."""
    config = load_settings(env_file=None)
    config.db.password = "p@ss/word:%?#"
    assert config.db.dsn().password == config.db.password
    assert config.db.password not in str(config.db.dsn())


def test_neutral_page_openapi_and_liveness_do_not_need_databases() -> None:
    """Import and serve the initial HTTP surface without optional broker imports."""
    application = create_app(load_settings(env_file=None))
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health/live").json() == {"status": "alive"}
        assert client.get("/swagger").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "503" in schema["paths"]["/health/ready"]["get"]["responses"]
        assert "src.broker.taskiq" not in sys.modules


@pytest.mark.parametrize("failed_backend", ["postgresql", "clickhouse"])
def test_readiness_reports_each_failed_database_without_secrets(failed_backend: str) -> None:
    """Keep liveness independent while exposing explicit per-backend 503 state."""
    application = create_app(load_settings(env_file=None))
    with TestClient(application) as client:
        database = client.portal.call(application.state.dishka_container.get, DatabaseHelper)
        analytics = client.portal.call(application.state.dishka_container.get, ClickHouseHelper)
        database.ping = AsyncMock()
        analytics.ping = AsyncMock()
        helper = database if failed_backend == "postgresql" else analytics
        helper.ping.side_effect = RuntimeError("secret-password-at-database")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()[failed_backend] == "down"
        assert response.json()["status"] == "not_ready"
        assert "secret-password" not in response.text
        assert client.get("/health/live").status_code == 200


async def test_probe_has_a_deadline() -> None:
    """A stalled backend cannot hold the readiness response indefinitely."""
    ping = AsyncMock(side_effect=lambda: None)
    assert await check_connection("test", ping, 0.01) == "up"
    assert await check_connection("test", asyncio.Event().wait, 0.01) == "down"


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_clickhouse_failed_initialization_closes_client(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    """Native client sessions are closed even when a readiness probe is cancelled."""
    client = AsyncMock()
    client.__aenter__.side_effect = failure("initialization interrupted")
    monkeypatch.setattr("src.analytics.helper.AsyncClient", lambda **kwargs: client)
    helper = ClickHouseHelper(load_settings(env_file=None).clickhouse)
    with pytest.raises(failure):
        await helper.get_client()
    client.close.assert_awaited_once()
    await helper.dispose()


@pytest.mark.parametrize("shutdown_failure", [None, RuntimeError, asyncio.CancelledError])
async def test_lifespan_releases_both_helpers_on_exception(
    monkeypatch: pytest.MonkeyPatch, shutdown_failure: type[BaseException] | None
) -> None:
    """Unwind both resources when request scope or another cleanup fails."""
    module = importlib.import_module("src.app.providers")
    database = AsyncMock()
    analytics = AsyncMock()
    if shutdown_failure is not None:
        analytics.dispose.side_effect = shutdown_failure("cleanup interrupted")
    monkeypatch.setattr(module, "DatabaseHelper", lambda *args, **kwargs: database)
    monkeypatch.setattr(module, "ClickHouseHelper", lambda *args, **kwargs: analytics)
    application = create_app(load_settings(env_file=None))
    expected = (
        asyncio.CancelledError
        if shutdown_failure is asyncio.CancelledError
        else ExitError
        if shutdown_failure is RuntimeError
        else RuntimeError
    )
    with pytest.raises(expected):
        async with application.router.lifespan_context(application):
            raise RuntimeError("request scope interrupted")
    database.dispose.assert_awaited_once()
    analytics.dispose.assert_awaited_once()


async def test_partial_startup_releases_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    """Release an acquired PG helper when subsequent resource creation fails."""
    module = importlib.import_module("src.app.providers")
    database = AsyncMock()
    monkeypatch.setattr(module, "DatabaseHelper", lambda *args, **kwargs: database)

    def fail_analytics(*args: object, **kwargs: object) -> None:
        """Simulate failure before the second resource can be registered."""
        raise RuntimeError("startup interrupted")

    monkeypatch.setattr(module, "ClickHouseHelper", fail_analytics)
    application = create_app(load_settings(env_file=None))
    with pytest.raises(RuntimeError):
        async with application.router.lifespan_context(application):
            pytest.fail("Startup unexpectedly completed")
    database.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    ("exception", "status", "error_status"),
    [
        (NotApiKey, 401, "UNAUTHENTICATED"),
        (InvalidApiKey, 403, "PERMISSION_DENIED"),
        (RuntimeError, 500, "INTERNAL"),
    ],
)
def test_exception_handlers_keep_the_framework_envelope(
    exception: type[Exception], status: int, error_status: str
) -> None:
    """Restore existing handler contracts without adding user authentication."""
    application = create_app(load_settings(env_file=None))

    async def fail(_request: Request) -> None:
        """Raise a controlled error containing a secret that must not be returned."""
        raise exception("secret-password")

    application.add_api_route("/test-error", fail)
    with TestClient(application, raise_server_exceptions=True) as client:
        response = client.get("/test-error")
    assert response.status_code == status
    assert response.json()["error"]["status"] == error_status
    assert response.json()["error"]["code"] == status
    assert "secret-password" not in response.text
