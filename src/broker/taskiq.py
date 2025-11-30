"""
Taskiq broker and scheduler setup.
"""
import logging

import taskiq_fastapi
from taskiq import TaskiqEvents, TaskiqState
from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_aio_pika import AioPikaBroker

from core.log_setup import setup_logging
from core.settings import settings

logger = logging.getLogger(__name__)

broker = AioPikaBroker(url=settings.rabbitmq.dsn())

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)

taskiq_fastapi.init(broker, "app.__main__:app")


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def on_worker_startup(state: TaskiqState) -> None:
    setup_logging(debug=False)
    logger.info("Worker startup complete, state %s", state)
