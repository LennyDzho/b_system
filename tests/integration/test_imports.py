"""Verify actual PG/CH publication, concurrency, interrupted writes, and commit ambiguity."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from src.api.v1.auth.schemas import CreateUserData
from src.api.v1.auth.service import AuthService
from src.app.__main__ import create_app
from src.core.infra.exceptions import ImportBusy, ImportFailed, PublicationUnknown
from src.core.settings import BASE_DIR, Settings
from src.database.helper import DatabaseHelper
from src.database.models import ImportRun
from src.database.repositories.auth import AuthRepo
from src.database.repositories.imports import IMPORT_LOCK, ImportRepo, import_lock
from src.services.imports.service import ImportService
from tests.import_helpers import write_sources

pytestmark = pytest.mark.integration


async def test_cancelled_lock_acknowledgement_does_not_leak_a_pooled_lock(
    environment: tuple[ImportService, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel after a real server acquisition and verify the backend lock is gone."""
    service, _ = environment
    original = AsyncConnection.scalar
    owner: list[int] = []

    async def cancelled(
        connection: AsyncConnection, statement: object, *args: object, **kwargs: object
    ) -> object:
        """Lose the acquisition acknowledgement after PostgreSQL has granted the lease."""
        result = await original(connection, statement, *args, **kwargs)
        if "pg_try_advisory_lock" in str(statement):
            owner.append(await original(connection, text("SELECT pg_backend_pid()")))
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(AsyncConnection, "scalar", cancelled)
    with pytest.raises(asyncio.CancelledError):
        async with import_lock(service.database):
            pytest.fail("Acquisition unexpectedly completed")
    monkeypatch.setattr(AsyncConnection, "scalar", original)
    async with service.database.async_engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM pg_locks WHERE pid=:pid AND locktype='advisory'"),
            {"pid": owner[0]},
        )
        assert count == 0
    async with import_lock(service.database):
        pass


async def active(service: ImportService) -> UUID | None:
    """Inspect the persisted pointer through a fresh PostgreSQL session."""
    async with service.database.session_factory() as session:
        return await ImportRepo(session).active_id()


async def test_unavailable_pg_reports_journal_failure_without_publication(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Use an actually unavailable TCP endpoint and reject success without a journal."""
    service, config = environment
    write_sources(tmp_path)
    unavailable = config.db.model_copy(update={"host": "127.0.0.1", "port": 1})
    broken = DatabaseHelper(unavailable.dsn(), timeout=0.2)
    try:
        with pytest.raises(ImportFailed, match="сохранить журнал"):
            await ImportService(broken, service.analytics).run(tmp_path, "test")
        assert await active(service) is None
        assert await attempts(service) == []
    finally:
        await broken.dispose()


async def attempts(service: ImportService) -> list[ImportRun]:
    """Read detached attempt states without trusting the service's return value."""
    async with service.database.session_factory() as session:
        return list(await session.scalars(select(ImportRun).order_by(ImportRun.created_at)))


async def test_real_supplied_dataset_and_repeated_cli(
    environment: tuple[ImportService, Settings],
) -> None:
    """Publish all source rows and prove CLI retry reuses them without new CH rows."""
    service, config = environment
    result = await service.run(BASE_DIR / "data", "test-operator")
    assert result["calculation_version"] == "idr-v1"
    assert result["summary"]["counts"] == {
        "raw": 357,
        "issues": 222,
        "properties.csv": 20,
        "reservations.csv": 150,
        "expenses.csv": 90,
        "payments.csv": 75,
        "commission_rules.csv": 20,
    }
    env = os.environ.copy()
    env.update(DB_NAME=config.db.database, CH_DATABASE=config.clickhouse.database)
    for arguments in ([], ["--history"]):
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "src.cli.import_data", *arguments],
            env=env,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        output = json.loads(completed.stdout)
        assert output["active_run_id"] == result["run_id"]
        if not arguments:
            assert output["reused"] is True
    client = await service.analytics.helper.get_client()
    assert (
        await client.query("SELECT count(), uniqExact((source,row_number)) FROM raw_rows")
    ).result_rows == [(357, 357)]
    assert len(await attempts(service)) == 1


async def test_changed_snapshot_old_retry_and_pipeline_version(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """New bytes/version publish separately; old repeated bytes never roll active back."""
    service, _ = environment
    write_sources(tmp_path)
    first = await service.run(tmp_path, "test")
    original = (tmp_path / "reservations.csv").read_bytes()
    (tmp_path / "reservations.csv").write_bytes(original.replace(b"1000", b"2000"))
    second = await service.run(tmp_path, "test")
    assert second["run_id"] != first["run_id"]
    assert second["summary"]["changes"]["reservations.csv"]["changed"] == ["booking-1"]
    (tmp_path / "reservations.csv").write_bytes(original)
    old_retry = await service.run(tmp_path, "test")
    assert old_retry["run_id"] == first["run_id"] and old_retry["active_run_id"] == second["run_id"]
    changed_version = ImportService(service.database, service.analytics, "canonical-v2-test")
    third = await changed_version.run(tmp_path, "test")
    assert third["run_id"] not in {first["run_id"], second["run_id"]}
    client = await service.analytics.helper.get_client()
    original_value = await client.query(
        "SELECT gross_booking_value FROM canonical_records WHERE run_id={id:UUID} AND source='reservations.csv'",
        parameters={"id": first["run_id"]},
    )
    assert str(original_value.result_rows[0][0]) == "1000.00"


@pytest.mark.parametrize("failure", ["missing", "schema"])
async def test_failed_preflight_journal(
    environment: tuple[ImportService, Settings], tmp_path: Path, failure: str
) -> None:
    """Unusable inputs retain failed metadata without touching the previous snapshot."""
    service, _ = environment
    write_sources(tmp_path)
    initial = await service.run(tmp_path, "test")
    path = tmp_path / "payments.csv"
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("not,the,schema\n", encoding="utf-8")
    with pytest.raises(ImportFailed):
        await service.run(tmp_path, "test")
    assert str(await active(service)) == initial["run_id"]
    failed = (await attempts(service))[-1]
    assert failed.status == "failed" and failed.error_code == "preflight_failed"
    assert failed.manifest["properties.csv"]["rows"] == 1


async def test_concurrent_importers_share_actual_pg_lease(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second importer cannot publish while another connection owns the lease."""
    service, _ = environment
    write_sources(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.analytics.write

    async def paused(*args: object) -> None:
        """Pause at the actual cross-database write boundary while holding the lock."""
        entered.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(service.analytics, "write", paused)
    running = asyncio.create_task(service.run(tmp_path, "first"))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        second = ImportService(service.database, service.analytics)
        with pytest.raises(ImportBusy):
            await second.run(tmp_path, "second")
    finally:
        release.set()
    first = await running
    retry = await service.run(tmp_path, "second")
    assert retry["run_id"] == first["run_id"] and len(await attempts(service)) == 1


@pytest.mark.parametrize("failure", ["after_write", "duplicate_rows"])
async def test_partial_or_duplicated_ch_write_is_never_published(
    environment: tuple[ImportService, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Keep the old pointer and use a fresh run after uncertain/duplicated CH writes."""
    service, _ = environment
    write_sources(tmp_path)
    first = await service.run(tmp_path, "test")
    path = tmp_path / "reservations.csv"
    path.write_bytes(path.read_bytes().replace(b"1000", b"3000"))
    original = service.analytics.write

    async def faulty(run_id: UUID, snapshot: object) -> None:
        """Inject an exception or duplicate physical write after genuine CH persistence."""
        await original(run_id, snapshot)
        if failure == "after_write":
            raise ConnectionError("simulated lost acknowledgement")
        columns, rows = service.analytics.rows(run_id, snapshot)["raw_rows"]
        client = await service.analytics.helper.get_client()
        await client.insert("raw_rows", rows[:1], column_names=columns)

    monkeypatch.setattr(service.analytics, "write", faulty)
    with pytest.raises(ImportFailed):
        await service.run(tmp_path, "test")
    failed = (await attempts(service))[-1]
    assert failed.status == "failed" and str(await active(service)) == first["run_id"]
    monkeypatch.setattr(service.analytics, "write", original)
    retried = await service.run(tmp_path, "test")
    assert retried["run_id"] != str(failed.run_id)
    assert str(await active(service)) == retried["run_id"]


@pytest.mark.parametrize("committed", [False, True])
async def test_publication_ack_failure_is_reconciled(
    environment: tuple[ImportService, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    """Read a fresh backend after an injected error around an actual PG commit."""
    service, _ = environment
    write_sources(tmp_path)
    original = ImportRepo.publish

    async def uncertain(repo: ImportRepo, *args: object) -> None:
        """Model an error immediately before or immediately after server-side commit."""
        if committed:
            await original(repo, *args)
        raise ConnectionError("simulated commit acknowledgement failure")

    monkeypatch.setattr(ImportRepo, "publish", uncertain)
    if committed:
        result = await service.run(tmp_path, "test")
        assert str(await active(service)) == result["run_id"]
        assert (await attempts(service))[-1].status == "published"
    else:
        with pytest.raises(ImportFailed, match="отсутствие commit"):
            await service.run(tmp_path, "test")
        assert await active(service) is None
        assert (await attempts(service))[-1].status == "failed"


async def test_unavailable_reconciliation_never_asserts_rollback(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expose unknown outcome while leaving an actually committed snapshot published."""
    service, _ = environment
    write_sources(tmp_path)
    original = ImportRepo.publish

    async def committed_but_lost(repo: ImportRepo, *args: object) -> None:
        """Commit real PG metadata, then simulate loss of the acknowledgement."""
        await original(repo, *args)
        raise ConnectionError("acknowledgement lost")

    monkeypatch.setattr(ImportRepo, "publish", committed_but_lost)
    monkeypatch.setattr(
        service,
        "reconcile",
        AsyncMock(side_effect=PublicationUnknown("journal unavailable; commit unknown")),
    )
    with pytest.raises(PublicationUnknown):
        await service.run(tmp_path, "test")
    run = (await attempts(service))[-1]
    assert run.status == "published" and await active(service) == run.run_id
    retry = await service.run(tmp_path, "test")
    assert retry["reused"] and retry["run_id"] == str(run.run_id)


async def test_cancellation_releases_lease_and_recovers_interrupted_attempt(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted post-write attempt stays invisible and is failed after lease recovery."""
    service, _ = environment
    write_sources(tmp_path)
    written = asyncio.Event()
    original = service.analytics.write

    async def interrupted(*args: object) -> None:
        """Pause after real writes so cancellation leaves an incomplete snapshot."""
        await original(*args)
        written.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.analytics, "write", interrupted)
    task = asyncio.create_task(service.run(tmp_path, "test"))
    await asyncio.wait_for(written.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    prior = (await attempts(service))[-1]
    assert prior.status == "running" and await active(service) is None
    monkeypatch.setattr(service.analytics, "write", original)
    result = await service.run(tmp_path, "test")
    assert result["run_id"] != str(prior.run_id)
    assert (await attempts(service))[0].error_code == "interrupted_before_publication"


async def test_terminated_lease_cannot_publish(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate the real lock-owning PG backend and forbid publication after reconnect."""
    service, _ = environment
    write_sources(tmp_path)
    original = service.analytics.verify

    async def terminate(*args: object) -> None:
        """Kill the owner only after CH verification, before the PG pointer switch."""
        await original(*args)
        async with service.database.async_engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype='advisory' AND objid=:key AND classid=0 AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                ),
                {"key": IMPORT_LOCK},
            )

    monkeypatch.setattr(service.analytics, "verify", terminate)
    with pytest.raises(ImportFailed):
        await service.run(tmp_path, "test")
    assert await active(service) is None


async def test_access_checks_the_published_dictionary(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Intersect viewer IDs with a real snapshot and reject assigned-but-absent IDs."""
    service, config = environment
    write_sources(tmp_path)
    await service.run(tmp_path, "test")
    async with service.database.session_factory() as session:
        await AuthService(AuthRepo(session), config.auth).create_user(
            CreateUserData(
                username="viewer",
                password="test-password-for-access",
                role="viewer",
                property_ids=["villa-1", "absent"],
            )
        )
    application = create_app(config)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "viewer", "password": "test-password-for-access"},
                )
            ).status_code == 200
            assert (await client.get("/api/v1/auth/me")).json()["access"]["property_ids"] == [
                "villa-1"
            ]
            assert (await client.get("/api/v1/auth/me?property_id=absent")).status_code == 404
            assert (await client.get("/api/v1/auth/me?property_id=foreign")).status_code == 403
