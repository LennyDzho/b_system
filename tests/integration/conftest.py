"""Use the same disposable real databases for pytest and browser integration."""

from collections.abc import AsyncIterator

import pytest

from src.core.settings import Settings
from src.services.imports.service import ImportService
from tests.real_environment import disposable_databases, migrated_environment


@pytest.fixture
async def databases() -> AsyncIterator[Settings]:
    """Create unique empty PG/CH databases without altering development data."""
    async with disposable_databases() as config:
        yield config


@pytest.fixture
async def environment(databases: Settings) -> AsyncIterator[tuple[ImportService, Settings]]:
    """Apply real migrations and own both resource helpers for one test."""
    async with migrated_environment(databases) as result:
        yield result
