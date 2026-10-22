"""Authorize CSV imports, immutable history and published snapshot comparisons."""

import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.quality import QualityRepo
from src.api.v1.auth.schemas import Identity
from src.core.infra.exceptions import (
    AuthError,
    DashboardError,
    ImportBusy,
    ImportFailed,
    PublicationUnknown,
    WebImportError,
)
from src.database.models import ImportRun
from src.database.repositories.imports import ImportRepo
from src.database.repositories.quality import HistoryRepo
from src.services.imports.records import SCHEMAS
from src.services.imports.service import ImportService

logger = logging.getLogger(__name__)


def require_import_admin(user: Identity) -> None:
    """Authorize writes before multipart parsing, filesystem staging or database mutation."""
    if user.role != "admin":
        raise AuthError(403, "Импорт доступен только администратору")


class ImportExecutionService:
    """Stage exact uploaded bytes and delegate every data rule to the existing importer."""

    def __init__(self, pipeline: ImportService) -> None:
        """Reuse lifespan-managed resources and the CLI publication/locking protocol."""
        self.pipeline = pipeline

    @staticmethod
    def stage(directory: Path, files: dict[str, bytes]) -> None:
        """Write only fixed schema names without overwriting the configured source directory."""
        for name in SCHEMAS:
            if name in files:
                (directory / name).write_bytes(files[name])

    async def execute(self, user: Identity, files: dict[str, bytes]) -> dict[str, Any]:
        """Own staging until the operation settles, even when the HTTP waiter is cancelled.

        A browser disconnect never proves rollback. The owned task retains its temporary
        files and database helpers; cancellation waits for it before leaving the request.
        No detached background job or second implementation of financial rules is created.
        """
        require_import_admin(user)
        if not files or not set(files).issubset(SCHEMAS):
            raise WebImportError(422, "Разрешены только CSV-файлы полного набора")
        task = asyncio.create_task(self.run(files, f"web:{user.id}:{user.username}"))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not task.cancelled():
                task.exception()  # Retrieve a settled failure without logging uploaded data.
            raise asyncio.CancelledError
        return task.result()

    async def run(self, files: dict[str, bytes], actor: str) -> dict[str, Any]:
        """Stage one full snapshot, preserving journaled preflight and uncertain outcomes."""
        try:
            with TemporaryDirectory(prefix="villa-upload-") as temporary:
                directory = Path(temporary)
                await asyncio.to_thread(self.stage, directory, files)
                result = await self.pipeline.run(directory, actor)
            if result["reused"]:
                message = (
                    "Набор уже импортирован. Активный снимок не изменён."
                    if result["active_run_id"] == result["run_id"]
                    else "Набор уже импортирован ранее. Активным оставлен более новый снимок."
                )
            else:
                message = "Новый снимок рассчитан, проверен и опубликован."
            return {**result, "message": message}
        except ImportBusy:
            raise WebImportError(
                409, "Другой импорт уже выполняется. Дождитесь его завершения.", outcome="busy"
            ) from None
        except PublicationUnknown as error:
            raise WebImportError(
                503,
                "Исход публикации не подтверждён. Проверьте историю перед повторной загрузкой.",
                outcome="unknown",
                run_id=error.run_id,
            ) from None
        except ImportFailed as error:
            preflight = error.code == "preflight_failed"
            raise WebImportError(
                422 if preflight else 503,
                "CSV-набор не прошёл проверку: " + str(error).partition(": ")[2]
                if preflight
                else "Импорт не подтверждён. Проверьте попытку в истории; при недоступной БД журнал мог не сохраниться.",
                outcome="failed" if error.run_id else "unavailable",
                run_id=error.run_id,
            ) from None
        except Exception as error:
            logger.error("Web import unavailable: %s", type(error).__name__)
            raise WebImportError(
                503,
                "Импорт недоступен. Успешная публикация не подтверждена.",
                outcome="unavailable",
            ) from None


class ImportHistoryService:
    """Keep administrator audit access separate from object-scoped diagnostic lists."""

    def __init__(self, session: AsyncSession, analytics: QualityRepo) -> None:
        """Reuse request-owned resources without creating an importer or recalculating facts."""
        self.session, self.analytics = session, analytics

    @staticmethod
    def require_admin(user: Identity) -> None:
        """Prevent any journal/manifest/actor/global-count disclosure to viewer roles."""
        if user.role != "admin":
            raise AuthError(403, "История импортов доступна только администратору")

    async def history(
        self, user: Identity, limit: int = 25, offset: int = 0, status: str | None = None
    ) -> dict[str, Any]:
        """Paginate the administrative journal, including failed and interrupted attempts."""
        self.require_admin(user)
        try:
            active = await ImportRepo(self.session).active_id()
            runs = await HistoryRepo(self.session).runs()
            selected = [run for run in runs if status is None or run.status == status]
            return {
                "active_run_id": str(active) if active else None,
                "total": len(selected),
                "limit": limit,
                "offset": offset,
                "items": [self.run_data(run) for run in selected[offset : offset + limit]],
            }
        except Exception as error:
            logger.error("Import history failure: %s", type(error).__name__)
            raise DashboardError("История импортов недоступна.") from None

    @staticmethod
    def run_data(run: ImportRun) -> dict[str, Any]:
        """Expose original audit metadata rather than reconstructing historical results."""
        return {
            "run_id": str(run.run_id),
            "status": run.status,
            "actor": run.actor,
            "created_at": run.created_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "pipeline_version": run.pipeline_version,
            "calculation_version": run.calculation_version,
            "dataset_hash": run.dataset_hash,
            "manifest": run.manifest,
            "summary": run.summary,
            "error_code": run.error_code,
            "previous_run_id": str(run.previous_run_id) if run.previous_run_id else None,
        }

    async def import_detail(
        self, user: Identity, run_id: UUID, base_run_id: UUID | None = None
    ) -> dict[str, Any]:
        """Compare published business hashes, retaining absence as an observation, not deletion."""
        self.require_admin(user)
        try:
            runs = {run.run_id: run for run in await HistoryRepo(self.session).runs()}
            current = runs.get(run_id)
            if current is None:
                raise AuthError(404, "Попытка импорта не найдена")
            base_id = base_run_id if base_run_id is not None else current.previous_run_id
            baseline = runs.get(base_id)
            comparison = None
            if base_run_id is not None and (
                baseline is None or baseline.status != "published" or current.status != "published"
            ):
                raise AuthError(404, "Для сравнения нужны два опубликованных снимка")
            if current.status == "published" and baseline and baseline.status == "published":
                before = await self.analytics.identities(baseline.run_id)
                after = await self.analytics.identities(current.run_id)
                comparison = {
                    source: {
                        "added": sorted(
                            after.get(source, {}).keys() - before.get(source, {}).keys()
                        ),
                        "changed": sorted(
                            key
                            for key in after.get(source, {}).keys() & before.get(source, {}).keys()
                            if after[source][key] != before[source][key]
                        ),
                        "absent_in_new_snapshot": sorted(
                            before.get(source, {}).keys() - after.get(source, {}).keys()
                        ),
                    }
                    for source in SCHEMAS
                }
            return {
                "run": self.run_data(current),
                "base_run_id": str(base_id) if base_id else None,
                "comparison": comparison,
                "notice": "Отсутствие в новом снимке не доказывает удаление операции в источнике. Изменение версии обработки может менять диагностику при неизменных исходных hashes.",
            }
        except AuthError:
            raise
        except Exception as error:
            logger.error("Import comparison failure: %s", type(error).__name__)
            raise DashboardError("Сравнение импортов недоступно.") from None
