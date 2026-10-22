"""Assemble the FastAPI domains, external web package and managed database resources."""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from dishka import Provider
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.analytics.helper import ClickHouseHelper
from src.api.exception_handlers import custom_exception_handler
from src.api.v1.health.routers import router as health_router
from src.api.v1.routers import router as api_router
from src.app.providers import build_container
from src.core.infra.exceptions import CustomException, DashboardError
from src.core.infra.middleware import SafeErrorMiddleware
from src.core.log_setup import setup_logging
from src.core.settings import BASE_DIR, Settings, settings
from src.database import DatabaseHelper
from web.imports import router as imports_web_router
from web.quality import router as quality_web_router
from web.routes import router as web_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Release all acquired resources on normal exit and partial startup failure."""
    async with AsyncExitStack() as resources:
        container = application.state.dishka_container
        resources.push_async_callback(container.close)
        config = await container.get(Settings)
        setup_logging(config.app.debug)
        await container.get(DatabaseHelper)
        await container.get(ClickHouseHelper)
        if config.rabbitmq.enabled:
            from src.broker import broker

            if broker is None:
                raise RuntimeError("Enabled broker is not configured")
            if not broker.is_worker_process:
                resources.push_async_callback(broker.shutdown)
                await broker.startup()
        logger.info("Application started.")
        yield
    logger.info("Application shutdown complete.")


def create_app(config: Settings = settings, *, providers: tuple[Provider, ...] = ()) -> FastAPI:
    """Assemble a validated Dishka graph with application-owned cleanup and test overrides."""
    application = FastAPI(
        debug=config.app.debug,
        lifespan=lifespan,
        title="Villa portfolio finance prototype",
        version="0.1.0",
        docs_url="/swagger",
        redoc_url=None,
        swagger_ui_parameters={"defaultModelsExpandDepth": -1},
    )
    setup_dishka(build_container(config, *providers), application)
    application.add_middleware(SafeErrorMiddleware, handler=custom_exception_handler)
    for exception_type in (CustomException, DashboardError, RequestValidationError, Exception):
        application.add_exception_handler(exception_type, custom_exception_handler)
    application.include_router(health_router)
    application.include_router(api_router)
    application.include_router(web_router)
    application.include_router(quality_web_router)
    application.include_router(imports_web_router)
    application.mount("/static", StaticFiles(directory=BASE_DIR / "web" / "static"), name="static")
    application.add_api_route("/", index, methods=["GET"], include_in_schema=False)
    return application


async def index() -> RedirectResponse:
    """Enter through login; a valid existing session continues to the account page."""
    return RedirectResponse("/login", status_code=303)


app = create_app()

if __name__ == "__main__":
    uvicorn.run("src.app.__main__:app", host="0.0.0.0", port=8000)
