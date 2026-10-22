"""Control PostgreSQL import metadata and the session-level publication lock."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from src.core.infra.exceptions import ImportBusy, ImportLockLost
from src.database.helper import DatabaseHelper
from src.database.models import DatasetState, ImportRun
from src.database.repositories.base import BaseRepo

logger = logging.getLogger(__name__)
IMPORT_LOCK = 73103801


@asynccontextmanager
async def import_lock(helper: DatabaseHelper) -> AsyncIterator[tuple[AsyncConnection, int]]:
    """Pin one physical PG connection and release or invalidate its session lock."""
    async with helper.async_engine.connect() as connection:
        acquired = False
        acquisition_settled = False
        try:
            acquired = bool(
                await connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": IMPORT_LOCK}
                )
            )
            acquisition_settled = True
            pid = await connection.scalar(text("SELECT pg_backend_pid()"))
            await connection.commit()
            if not acquired:
                raise ImportBusy("Другой импорт уже выполняется; повторите позже")
            yield connection, int(pid)
        finally:
            if not acquisition_settled:
                # A cancelled acknowledgement can hide a server-acquired session lock.
                await connection.invalidate()
            elif acquired:
                try:
                    if connection.in_transaction():
                        await connection.rollback()
                    if not connection.invalidated:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(:key)"), {"key": IMPORT_LOCK}
                        )
                        await connection.commit()
                except BaseException as error:
                    logger.warning(
                        "Import lease cleanup invalidated connection: %s", type(error).__name__
                    )
                    await connection.invalidate()
                    if not isinstance(error, Exception):
                        raise


class ImportRepo(BaseRepo):
    """Keep journal mutations and atomic pointer switching in PostgreSQL."""

    async def active_id(self) -> UUID | None:
        """Read the one active ID; absence means no snapshot has been published."""
        return await self.session.scalar(
            select(DatasetState.active_run_id).where(DatasetState.id == 1)
        )

    async def assert_lock(self, backend_pid: int) -> None:
        """Prevent a reconnect from silently continuing after loss of serialization."""
        result = await self.session.execute(
            text(
                "SELECT pg_backend_pid(), EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid() AND classid = 0 AND objid = :key AND objsubid = 1 AND granted)"
            ),
            {"key": IMPORT_LOCK},
        )
        pid, held = result.one()
        if pid != backend_pid or not held:
            raise ImportLockLost("Потеряна блокировка импортёра; снимок не опубликован")

    async def recover_interrupted(self) -> None:
        """Mark only confirmed unpublished running attempts after acquiring the lease."""
        active = await self.active_id()
        query = update(ImportRun).where(ImportRun.status == "running")
        if active is not None:
            query = query.where(ImportRun.run_id != active)
        await self.session.execute(
            query.values(
                status="failed", error_code="interrupted_before_publication", finished_at=func.now()
            )
        )
        await self.commit()

    async def published(
        self, dataset_hash: str, version: str, calculation_version: str | None = None
    ) -> ImportRun | None:
        """Find a completed exact processing key, treating calculation NULL explicitly."""
        return await self.session.scalar(
            select(ImportRun)
            .where(
                ImportRun.dataset_hash == dataset_hash,
                ImportRun.pipeline_version == version,
                ImportRun.calculation_version == calculation_version,
                ImportRun.status == "published",
            )
            .order_by(ImportRun.created_at)
            .limit(1)
        )

    async def fail(self, run_id: UUID, code: str) -> None:
        """Finish an unpublished attempt without changing the active pointer."""
        await self.session.execute(
            update(ImportRun)
            .where(ImportRun.run_id == run_id, ImportRun.status == "running")
            .values(status="failed", error_code=code[:128], finished_at=func.now())
        )
        await self.commit()

    async def publish(self, run_id: UUID, summary: dict[str, Any], backend_pid: int) -> None:
        """Commit published status and the active pointer in the same transaction."""
        await self.assert_lock(backend_pid)
        run = await self.session.get(ImportRun, run_id)
        if run is None or run.status != "running":
            raise RuntimeError("Import attempt is not running")
        state = await self.session.get(DatasetState, 1)
        if state is None:
            self.add(DatasetState(id=1, active_run_id=run_id))
        else:
            state.active_run_id = run_id
        run.status = "published"
        run.summary = summary
        run.finished_at = func.now()
        await self.commit()
