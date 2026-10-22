"""Publish canonical snapshots across PG/CH without claiming a shared transaction."""

import asyncio
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.imports import AnalyticsImportRepo
from src.core.infra.exceptions import ImportBusy, ImportFailed, PublicationUnknown
from src.database.helper import DatabaseHelper
from src.database.models import ImportRun
from src.database.repositories.imports import ImportRepo, import_lock
from src.finance.policy import CALCULATION_VERSION, aggregate, calculate
from src.quality.diagnostics import diagnose, issue_counts
from src.services.imports.parser import canonicalize, capture
from src.services.imports.records import SCHEMAS, Snapshot

PIPELINE_VERSION = "quality-v1"


class ImportService:
    """Coordinate captured inputs, repository checks, and one-way publication."""

    def __init__(
        self,
        database: DatabaseHelper,
        analytics: AnalyticsImportRepo,
        pipeline_version: str = PIPELINE_VERSION,
        calculation_version: str | None = CALCULATION_VERSION,
    ) -> None:
        """Reuse resource helpers; processing version participates in idempotency."""
        self.database = database
        self.analytics = analytics
        self.pipeline_version = pipeline_version
        self.calculation_version = calculation_version

    async def run(self, directory: Path, actor: str) -> dict[str, Any]:
        """Import once under a PG lease; unchanged old snapshots never roll active back."""
        if not actor.strip() or len(actor) > 128:
            raise ImportFailed("Actor должен содержать 1–128 символов")
        snapshot = await asyncio.to_thread(capture, directory)
        try:
            async with import_lock(self.database) as (connection, pid):
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    repo = ImportRepo(session)
                    await repo.recover_interrupted()
                    dataset_hash = snapshot.dataset_hash()
                    if not snapshot.errors and dataset_hash:
                        prior = await repo.published(
                            dataset_hash, self.pipeline_version, self.calculation_version
                        )
                        if prior is not None:
                            return await self.result(repo, prior, reused=True)
                    run = ImportRun(
                        run_id=uuid4(),
                        dataset_hash=dataset_hash,
                        pipeline_version=self.pipeline_version,
                        calculation_version=self.calculation_version,
                        status="failed" if snapshot.errors else "running",
                        actor=actor,
                        previous_run_id=await repo.active_id(),
                        manifest=snapshot.manifest,
                        summary={"preflight_errors": snapshot.errors} if snapshot.errors else {},
                        error_code="preflight_failed" if snapshot.errors else None,
                        finished_at=datetime.now(UTC) if snapshot.errors else None,
                    )
                    repo.add(run)
                    await repo.commit()
                    if snapshot.errors:
                        raise ImportFailed(
                            f"{run.run_id}: " + "; ".join(snapshot.errors),
                            run_id=run.run_id,
                            code="preflight_failed",
                        )
                    try:
                        snapshot = await asyncio.to_thread(canonicalize, snapshot)
                        if self.calculation_version is not None:
                            snapshot.financial_facts = await asyncio.to_thread(calculate, snapshot)
                            await asyncio.to_thread(diagnose, snapshot)
                        summary = await self.summary(snapshot, run.previous_run_id)
                        run.summary = summary
                        await repo.commit()
                        await self.analytics.write(run.run_id, snapshot)
                        await self.analytics.verify(run.run_id, snapshot)
                    except Exception as error:
                        await self.mark_failed(run.run_id, type(error).__name__)
                        raise ImportFailed(
                            f"{run.run_id}: импорт не опубликован ({type(error).__name__})",
                            run_id=run.run_id,
                        ) from None
                    try:
                        await repo.publish(run.run_id, summary, pid)
                    except Exception:
                        # End the old backend before a fresh lease establishes a settled outcome.
                        await connection.invalidate()
                        return await self.reconcile(run.run_id)
                    return await self.result(repo, run, reused=False, published_now=True)
        except (ImportFailed, PublicationUnknown):
            raise
        except Exception as error:
            if isinstance(error, ImportBusy):
                raise
            raise ImportFailed(
                f"Не удалось завершить импорт/сохранить журнал ({type(error).__name__})"
            ) from None

    async def summary(self, snapshot: Snapshot, previous: UUID | None) -> dict[str, Any]:
        """Describe canonical counts and source changes, never source-system deletions."""
        old = (
            await self.analytics.fingerprints(previous)
            if previous
            else {name: {} for name in SCHEMAS}
        )
        current = {
            name: {
                row.business_key: row.payload_hash
                for row in snapshot.canonical
                if row.source == name
            }
            for name in SCHEMAS
        }
        changes = {}
        for source in SCHEMAS:
            before, after = old[source], current[source]
            changes[source] = {
                "added": sorted(after.keys() - before.keys()),
                "changed": sorted(
                    key for key in after.keys() & before.keys() if after[key] != before[key]
                ),
                "absent_in_new_snapshot": sorted(before.keys() - after.keys()),
            }
        return {
            "counts": snapshot.counts(),
            "issues_by_code": dict(
                sorted(Counter(issue.code for issue in snapshot.issues).items())
            ),
            "quality": issue_counts(snapshot.issues),
            "affected_rows": issue_counts(snapshot.issues)["affected_rows"],
            "canonical_type_counts": dict(
                sorted(Counter(record.type_status for record in snapshot.canonical).items())
            ),
            "calculation_version": self.calculation_version,
            "financial_inclusion": "calculated" if self.calculation_version else "not_calculated",
            "financial": aggregate(snapshot.financial_facts) if self.calculation_version else None,
            "partial": bool(snapshot.issues),
            "affected_entities": issue_counts(snapshot.issues)["affected_entities"],
            "changes": changes,
        }

    async def mark_failed(self, run_id: UUID, code: str) -> None:
        """Persist a definite pre-publication failure through a fresh session."""
        try:
            async with self.database.session_factory() as session:
                await ImportRepo(session).fail(run_id, code)
        except Exception as error:
            raise ImportFailed(
                f"{run_id}: сбой до публикации; журнал недоступен ({type(error).__name__})",
                run_id=run_id,
            ) from None

    async def reconcile(self, run_id: UUID) -> dict[str, Any]:
        """Acquire a new lease and distinguish committed, uncommitted, and unknown outcomes."""
        try:
            async with import_lock(self.database) as (connection, _pid):
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    repo = ImportRepo(session)
                    run = await session.get(ImportRun, run_id)
                    active = await repo.active_id()
                    if run is not None and run.status == "published":
                        return await self.result(repo, run, reused=False)
                    if run is not None and active != run_id:
                        await repo.fail(run_id, "publication_not_committed")
                        raise ImportFailed(
                            f"{run_id}: подтверждено отсутствие commit публикации", run_id=run_id
                        )
                    raise PublicationUnknown(
                        f"{run_id}: противоречивое состояние публикации", run_id=run_id
                    )
        except ImportFailed:
            raise
        except Exception as error:
            raise PublicationUnknown(
                f"{run_id}: исход публикации неизвестен; повторно сверьте журнал ({type(error).__name__})",
                run_id=run_id,
            ) from None

    async def result(
        self, repo: ImportRepo, run: ImportRun, reused: bool, published_now: bool = False
    ) -> dict[str, Any]:
        """Return a persisted result and current pointer without mutating previous snapshots."""
        active = run.run_id if published_now else await repo.active_id()
        return {
            "run_id": str(run.run_id),
            "active_run_id": str(active) if active else None,
            "status": run.status,
            "reused": reused,
            "pipeline_version": run.pipeline_version,
            "calculation_version": run.calculation_version,
            "summary": run.summary,
        }

    async def history(self) -> dict[str, Any]:
        """Expose operator-readable journal metadata, including failed/interrupted attempts."""
        async with self.database.session_factory() as session:
            repo = ImportRepo(session)
            active = await repo.active_id()
            runs = (
                await session.scalars(select(ImportRun).order_by(ImportRun.created_at.desc()))
            ).all()
            return {
                "active_run_id": str(active) if active else None,
                "runs": [
                    {
                        "run_id": str(run.run_id),
                        "status": run.status,
                        "actor": run.actor,
                        "created_at": run.created_at.isoformat(),
                        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                        "dataset_hash": run.dataset_hash,
                        "pipeline_version": run.pipeline_version,
                        "calculation_version": run.calculation_version,
                        "previous_run_id": str(run.previous_run_id)
                        if run.previous_run_id
                        else None,
                        "manifest": run.manifest,
                        "summary": run.summary,
                        "error_code": run.error_code,
                    }
                    for run in runs
                ],
            }
