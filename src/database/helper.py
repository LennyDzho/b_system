"""
Database helper module for managing connections and sessions.
"""
import logging
from typing import AsyncGenerator

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)


logger = logging.getLogger(__name__)


class DatabaseHelper:
    """
    Helper class for managing database connections and sessions.
    """

    def __init__(
        self,
        url: str,
        echo: bool = False,
        echo_pool: bool = False,
        pool_size: int = 10,
        max_overflow: int = 10,
    ) -> None:
        """
        Initializes the DatabaseHelper with synchronous and asynchronous engines.
        """
        self.engine: Engine = create_engine(
            url,
            echo=echo,
            echo_pool=echo_pool,
            pool_size=pool_size,
            max_overflow=max_overflow,
        )

        self.async_engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            echo_pool=echo_pool,
            pool_size=pool_size,
            max_overflow=max_overflow,
        )

        self.session_factory = async_sessionmaker(
            bind=self.async_engine, expire_on_commit=False, autoflush=False
        )

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Asynchronous generator that yields a new database session.
        """
        logger.debug("Creating new database session...")
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        """
        Disposes of the asynchronous engine, closing all connections.
        """
        logger.info("Disposing database...")
        await self.async_engine.dispose()
