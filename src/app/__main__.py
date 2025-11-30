import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from api.exception_handlers import (
    general_exception_handler,
    invalid_api_key_exception_handler,
    not_api_key_exception_handler,
)
from broker import broker
from core.infra.exceptions import InvalidApiKey, NotApiKey
from core.log_setup import setup_logging
from core.settings import settings
from database import db_helper

logger = logging.getLogger(__name__)

setup_logging(settings.app.debug)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting application....")

    if not broker.is_worker_process:
        await broker.startup()

    yield
    if not broker.is_worker_process:
        await broker.shutdown()
    await db_helper.dispose()

    logger.info("Application shutdown complete.")


app = FastAPI(
    debug=settings.app.debug,
    lifespan=lifespan,
    title="Testing app",
    version="0.1.0beta",
    docs_url="/swagger",
    redoc_url=None,
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
)


app.add_exception_handler(NotApiKey, not_api_key_exception_handler)
app.add_exception_handler(InvalidApiKey, invalid_api_key_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)


@app.get("/", include_in_schema=False)
async def redirect_to_docs() -> RedirectResponse:
    return RedirectResponse("/docs")


if __name__ == "__main__":
    with suppress(KeyboardInterrupt, SystemExit):
        uvicorn.run(app, host="0.0.0.0", port=8000)


