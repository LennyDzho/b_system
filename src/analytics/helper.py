"""Own a lazily initialized native asynchronous ClickHouse client."""

import asyncio
import logging

from clickhouse_connect.driver import AsyncClient

from src.core.settings import ClickHouseConfig

logger = logging.getLogger(__name__)


class ClickHouseHelper:
    """Reuse one HTTP client per lifespan without blocking the event loop."""

    def __init__(self, config: ClickHouseConfig) -> None:
        """Defer network I/O so liveness does not need an available database."""
        self.config = config
        self._client: AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def get_client(self) -> AsyncClient:
        """Initialize once; failed connections remain retryable for readiness."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("ClickHouse helper is closed")
            if self._client is None:
                client = AsyncClient(
                    interface="https" if self.config.secure else "http",
                    host=self.config.host,
                    port=self.config.port,
                    username=self.config.user,
                    password=self.config.password,
                    database=self.config.database,
                    connect_timeout=self.config.timeout,
                    send_receive_timeout=self.config.timeout,
                    query_retries=0,
                    autogenerate_session_id=False,
                    tz_mode="aware",
                )
                try:
                    await client.__aenter__()
                except BaseException:
                    # The factory in pinned 1.0.1 does not close on cancellation.
                    await client.close()
                    raise
                self._client = client
            return self._client

    async def ping(self) -> None:
        """Query the configured database, validating credentials and access."""
        client = await self.get_client()
        await client.query("SELECT 1")

    async def dispose(self) -> None:
        """Close the HTTP pool even when another lifespan resource fails."""
        async with self._lock:
            self._closed = True
            if self._client is not None:
                logger.info("Disposing ClickHouse connections.")
                await self._client.close()
                self._client = None
