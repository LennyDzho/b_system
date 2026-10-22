"""Probe actual backend availability independently of HTTP response handling."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from src.analytics.helper import ClickHouseHelper
from src.api.v1.health.schemas import LivenessResponse, ReadinessResponse
from src.database.helper import DatabaseHelper

logger = logging.getLogger(__name__)


async def check_connection(
    name: str, ping: Callable[[], Awaitable[None]], timeout: float
) -> Literal["up", "down"]:
    """Bound a backend probe and log only the backend and failure type."""
    try:
        async with asyncio.timeout(timeout):
            await ping()
    except Exception as exception:
        logger.warning("%s readiness failed: %s", name, type(exception).__name__)
        return "down"
    return "up"


class HealthService:
    """Separate live process state from bounded concurrent database queries."""

    def __init__(
        self, database: DatabaseHelper, analytics: ClickHouseHelper, timeout: float
    ) -> None:
        """Use lifespan-managed helpers and the configured readiness deadline."""
        self.database, self.analytics, self.timeout = database, analytics, timeout

    @staticmethod
    def liveness() -> LivenessResponse:
        """Return process liveness without touching external resources."""
        return LivenessResponse()

    async def readiness(self) -> ReadinessResponse:
        """Read both real database probes concurrently without exposing private errors."""
        postgresql, clickhouse = await asyncio.gather(
            check_connection("PostgreSQL", self.database.ping, self.timeout),
            check_connection("ClickHouse", self.analytics.ping, self.timeout),
        )
        return ReadinessResponse(
            status="ready" if postgresql == clickhouse == "up" else "not_ready",
            postgresql=postgresql,
            clickhouse=clickhouse,
        )
