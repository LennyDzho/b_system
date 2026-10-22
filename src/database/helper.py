"""Manage the framework's asynchronous PostgreSQL engine and sessions."""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)


class DatabaseHelper:
    """Own one async engine; callers keep control of transaction boundaries."""

    def __init__(
        self,
        url: str | URL,
        echo: bool = False,
        echo_pool: bool = False,
        pool_size: int = 10,
        max_overflow: int = 10,
        timeout: float = 3,
    ) -> None:
        """Configure a lazy engine with finite connection and query timeouts."""
        self.async_engine = create_async_engine(
            url,
            echo=echo,
            echo_pool=echo_pool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_pre_ping=True,
            pool_timeout=timeout,
            connect_args={"timeout": timeout, "command_timeout": timeout},
        )
        self.session_factory = async_sessionmaker(
            bind=self.async_engine, expire_on_commit=False, autoflush=False
        )

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """Yield a separate session and release it without an implicit commit."""
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> None:
        """Execute a real query and propagate connection or server failures."""
        async with self.async_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Release the connection pool on shutdown, including failed startup."""
        logger.info("Disposing PostgreSQL connections.")
        await self.async_engine.dispose()
