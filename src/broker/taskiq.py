"""
Taskiq broker and scheduler setup.
"""

import logging

import taskiq_fastapi
from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_aio_pika import AioPikaBroker

from src.core.log_setup import setup_logging
from src.core.settings import settings

logger = logging.getLogger(__name__)

broker = AioPikaBroker(url=settings.rabbitmq.dsn()) if settings.rabbitmq.enabled else None


async def on_worker_startup(state: TaskiqState) -> None:
    """Configure worker logging only when the optional transport is enabled."""
    setup_logging(debug=False)
    logger.info("Worker startup complete.")


scheduler: TaskiqScheduler | None = None
if broker is not None:
    scheduler = TaskiqScheduler(broker=broker, sources=[LabelScheduleSource(broker)])
    taskiq_fastapi.init(broker, "src.app.__main__:app")
    broker.on_event(TaskiqEvents.WORKER_STARTUP)(on_worker_startup)
