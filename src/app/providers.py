"""Define Dishka lifetimes for shared database resources and request-local use cases."""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass

from dishka import AsyncContainer, Provider, Scope, from_context, make_async_container, provide
from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.dashboard import DashboardRepo
from src.analytics.helper import ClickHouseHelper
from src.analytics.imports import AnalyticsImportRepo
from src.analytics.quality import QualityRepo
from src.api.v1.auth.service import AuthService
from src.api.v1.finance.service import DashboardService
from src.api.v1.health.service import HealthService
from src.api.v1.imports.service import ImportExecutionService, ImportHistoryService
from src.api.v1.quality.service import QualityService
from src.core.settings import AuthConfig, Settings
from src.database.helper import DatabaseHelper
from src.database.repositories.auth import AuthRepo
from src.database.repositories.imports import ImportRepo
from src.services.imports.service import ImportService


@dataclass(frozen=True)
class DatabaseResources:
    """Keep both APP resources under one cancellation-safe finalization stack."""

    database: DatabaseHelper
    analytics: ClickHouseHelper


class ApplicationProvider(Provider):
    """Own one configuration and both lazy database pools for each application."""

    scope = Scope.APP
    settings = from_context(Settings)

    @provide
    def auth_config(self, config: Settings) -> AuthConfig:
        """Expose the same cookie and Origin policy to HTTP guards and auth services."""
        return config.auth

    @provide
    async def resources(self, config: Settings) -> AsyncIterator[DatabaseResources]:
        """Attempt both cleanups on partial startup, ordinary errors and cancellation.

        Dishka 1.7.2 stops iterating APP finalizers on BaseException. A single
        resource finalizer with AsyncExitStack still unwinds PostgreSQL when
        ClickHouse disposal raises CancelledError. Neither helper connects eagerly.
        """
        async with AsyncExitStack() as stack:
            database = DatabaseHelper(config.db.dsn(), timeout=config.db.timeout)
            stack.push_async_callback(database.dispose)
            analytics = ClickHouseHelper(config.clickhouse)
            stack.push_async_callback(analytics.dispose)
            yield DatabaseResources(database, analytics)

    @provide
    def database(self, resources: DatabaseResources) -> DatabaseHelper:
        """Resolve the application PostgreSQL pool with the shared resource owner."""
        return resources.database

    @provide
    def analytics(self, resources: DatabaseResources) -> ClickHouseHelper:
        """Resolve the application ClickHouse pool with the shared resource owner."""
        return resources.analytics


class RepositoryProvider(Provider):
    """Share one PostgreSQL session across all repositories within an HTTP request."""

    scope = Scope.REQUEST
    auth = provide(AuthRepo)
    imports = provide(ImportRepo)
    dashboard = provide(DashboardRepo)
    quality = provide(QualityRepo)
    analytics_imports = provide(AnalyticsImportRepo)

    @provide
    async def session(self, database: DatabaseHelper) -> AsyncIterator[AsyncSession]:
        """Close the request session on every exit; use cases retain transaction ownership."""
        async with database.session_factory() as session:
            yield session


class ServiceProvider(Provider):
    """Construct HTTP use cases without coupling their implementation to Dishka or FastAPI."""

    scope = Scope.REQUEST
    auth = provide(AuthService)
    dashboard = provide(DashboardService)
    quality = provide(QualityService)
    history = provide(ImportHistoryService)
    execution = provide(ImportExecutionService)

    @provide
    def pipeline(self, database: DatabaseHelper, analytics: AnalyticsImportRepo) -> ImportService:
        """Keep pipeline versions and publication rules identical to the trusted CLI."""
        return ImportService(database, analytics)

    @provide
    def health(
        self, database: DatabaseHelper, analytics: ClickHouseHelper, config: Settings
    ) -> HealthService:
        """Apply the configured probe deadline without acquiring a request transaction."""
        return HealthService(database, analytics, config.app.readiness_timeout)


def build_container(config: Settings, *overrides: Provider) -> AsyncContainer:
    """Validate the application graph, allowing explicit typed provider overrides in tests."""
    return make_async_container(
        ApplicationProvider(),
        RepositoryProvider(),
        ServiceProvider(),
        *overrides,
        context={Settings: config},
    )
