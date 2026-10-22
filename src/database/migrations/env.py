"""Run Alembic with the framework's asyncpg engine and ORM model registry."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from src.core.settings import settings
from src.database.helper import DatabaseHelper
from src.database.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Future model modules are explicitly exported by src.database.models.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate PostgreSQL SQL without opening a connection or rendering a DSN."""
    context.configure(
        url=settings.db.dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Bind Alembic to a synchronous facade of the real async connection."""
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect with asyncpg and dispose the engine even on migration failure."""
    helper = DatabaseHelper(settings.db.dsn(), timeout=settings.db.timeout)
    try:
        async with helper.async_engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await helper.dispose()


def run_migrations_online() -> None:
    """Support the CLI and externally supplied connections for integration tests."""
    connection = config.attributes.get("connection")
    if connection is not None:
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
