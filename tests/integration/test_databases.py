"""Verify real connections and repeatable migrations in disposable databases."""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

from src.analytics.helper import ClickHouseHelper
from src.analytics.migrations.__main__ import SQL_DIR, migrate
from src.app.__main__ import create_app
from src.core.settings import BASE_DIR, Settings
from src.database.helper import DatabaseHelper

pytestmark = pytest.mark.integration


async def test_real_readiness_and_resource_shutdown(databases: Settings) -> None:
    """Use both actual drivers through HTTP and close their pools on shutdown."""
    application = create_app(databases)
    async with application.router.lifespan_context(application):
        database = await application.state.dishka_container.get(DatabaseHelper)
        analytics = await application.state.dishka_container.get(ClickHouseHelper)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            responses = await asyncio.gather(
                client.get("/health/ready"), client.get("/health/ready")
            )
            for response in responses:
                assert response.status_code == 200
                assert response.json() == {
                    "status": "ready",
                    "postgresql": "up",
                    "clickhouse": "up",
                }
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/swagger")).status_code == 200
    assert analytics._client is None
    assert database.async_engine.pool.checkedin() == 0


async def test_empty_database_migrations_repeat_and_checksums(
    databases: Settings, tmp_path: Path
) -> None:
    """Run PG migrations twice and CH DDL twice, then reject edited SQL history."""
    environment = os.environ.copy()
    environment.update(
        DB_NAME=databases.db.database,
        DB_HOST=databases.db.host,
        DB_PORT=str(databases.db.port),
        DB_USER=databases.db.user,
        DB_PASSWORD=databases.db.password,
    )
    for arguments in [("upgrade", "head"), ("upgrade", "head"), ("check",)]:
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *arguments],
            cwd=BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    pg = DatabaseHelper(databases.db.dsn())
    ch = ClickHouseHelper(databases.clickhouse)
    try:
        async with pg.async_engine.connect() as connection:
            tables = await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            assert set(tables.scalars()) == {
                "alembic_version",
                "users",
                "user_sessions",
                "user_property_access",
                "import_runs",
                "dataset_state",
            }
        assert await migrate(ch) == sorted(path.name for path in SQL_DIR.glob("*.sql"))
        assert await migrate(ch) == []
        client = await ch.get_client()
        migration_count = len(list(SQL_DIR.glob("*.sql")))
        assert (await client.query("SELECT count() FROM schema_migrations")).result_rows == [
            (migration_count,)
        ]
        assert set(row[0] for row in (await client.query("SHOW TABLES")).result_rows) == {
            "schema_migrations",
            "raw_rows",
            "canonical_records",
            "quality_issues",
            "financial_facts",
        }
        path = tmp_path / "0001_schema_migrations.sql"
        for source in SQL_DIR.glob("*.sql"):
            shutil.copyfile(source, tmp_path / source.name)
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        assert await migrate(ch, tmp_path) == []
        path.write_text("DROP TABLE schema_migrations", encoding="utf-8")
        with pytest.raises(RuntimeError, match="Applied migration was changed"):
            await migrate(ch, tmp_path)
        assert (await client.query("SELECT count() FROM schema_migrations")).result_rows == [
            (migration_count,)
        ]
    finally:
        await ch.dispose()
        await pg.dispose()


@pytest.mark.parametrize("unavailable", ["postgresql", "clickhouse"])
async def test_real_connection_failure_preserves_liveness(
    databases: Settings, unavailable: str
) -> None:
    """Use real failed TCP connections to distinguish each unavailable backend."""
    config = databases.model_copy(deep=True)
    if unavailable == "postgresql":
        config.db.host, config.db.port = "127.0.0.1", 1
    else:
        config.clickhouse.host, config.clickhouse.port = "127.0.0.1", 1
    application = create_app(config)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            response = await client.get("/health/ready")
            assert response.status_code == 503
            assert response.json()[unavailable] == "down"
            other = "clickhouse" if unavailable == "postgresql" else "postgresql"
            assert response.json()[other] == "up"
            assert (await client.get("/health/live")).status_code == 200
            assert config.db.password not in response.text
            assert config.clickhouse.password not in response.text
