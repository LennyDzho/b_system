"""Share disposable real PG/CH databases across infrastructure integration tests."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from src.analytics.helper import ClickHouseHelper
from src.analytics.imports import AnalyticsImportRepo
from src.analytics.migrations.__main__ import migrate
from src.core.settings import BASE_DIR, Settings, load_settings
from src.database.helper import DatabaseHelper
from src.services.imports.service import ImportService


@asynccontextmanager
async def disposable_databases() -> AsyncIterator[Settings]:
    """Create unique empty PG/CH databases without altering development data."""
    config = load_settings(env_file=None)
    name = "e01_test_" + uuid4().hex
    pg_admin = DatabaseHelper(config.db.dsn())
    ch_admin = ClickHouseHelper(config.clickhouse)
    pg_created = ch_created = False
    try:
        async with pg_admin.async_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            pg_created = True
        client = await ch_admin.get_client()
        await client.command(f"CREATE DATABASE {name}")
        ch_created = True
        isolated = config.model_copy(deep=True)
        isolated.db.database = isolated.clickhouse.database = name
        yield isolated
    finally:
        try:
            if ch_created:
                client = await ch_admin.get_client()
                await client.command(f"DROP DATABASE {name} SYNC")
        finally:
            await ch_admin.dispose()
            try:
                if pg_created:
                    async with pg_admin.async_engine.connect() as connection:
                        connection = await connection.execution_options(
                            isolation_level="AUTOCOMMIT"
                        )
                        await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            finally:
                await pg_admin.dispose()


@asynccontextmanager
async def migrated_environment(
    databases: Settings,
) -> AsyncIterator[tuple[ImportService, Settings]]:
    """Migrate unique empty databases and own their service resources for one test."""
    pg = DatabaseHelper(databases.db.dsn())
    ch = ClickHouseHelper(databases.clickhouse)
    try:
        async with pg.async_engine.begin() as connection:

            def upgrade(sync_connection: Connection) -> None:
                """Apply the real Alembic head on the disposable test database."""
                config = Config(str(BASE_DIR / "alembic.ini"))
                config.attributes["connection"] = sync_connection
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
        await migrate(ch)
        yield ImportService(pg, AnalyticsImportRepo(ch)), databases
    finally:
        await ch.dispose()
        await pg.dispose()
