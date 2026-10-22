"""Verify browser-import HTTP contracts using real PG/CH publication and failure boundaries."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from dishka import Provider, Scope
from fastapi import FastAPI

from src.analytics.imports import AnalyticsImportRepo
from src.api.v1.imports.service import ImportExecutionService
from src.app.__main__ import create_app
from src.core.settings import BASE_DIR, Settings
from src.database.helper import DatabaseHelper
from src.database.repositories.imports import ImportRepo, import_lock
from src.services.imports.records import SCHEMAS
from src.services.imports.service import ImportService
from tests.import_helpers import write_sources
from tests.integration.test_auth import seed
from tests.integration.test_imports import attempts
from tests.test_finance import EXPECTED, METRICS

pytestmark = pytest.mark.integration


def multipart(files: dict[str, bytes]) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Encode unchanged source bytes under fixed multipart field names."""
    return [("files", (name, content, "text/csv")) for name, content in files.items()]


def source_bytes(directory: Path) -> dict[str, bytes]:
    """Capture the five source files without editing test or supplied datasets."""
    return {name: (directory / name).read_bytes() for name in SCHEMAS}


@asynccontextmanager
async def web_client(
    importer: ImportService, config: Settings, *, providers: tuple[Provider, ...] = ()
) -> AsyncIterator[tuple[httpx.AsyncClient, FastAPI]]:
    """Log in to a real lifespan-managed app as an administrator."""
    password = await seed(importer.database, config, "web-admin", "admin", [])
    app = create_app(config, providers=providers)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
            timeout=60,
        ) as client:
            assert (
                await client.post(
                    "/api/v1/auth/login", json={"username": "web-admin", "password": password}
                )
            ).status_code == 200
            yield client, app


async def test_actual_upload_controls_manifest_audit_and_repeat(
    environment: tuple[ImportService, Settings],
) -> None:
    """Import the real supplied CSVs through HTTP and preserve controls, bytes and idempotency."""
    importer, config = environment
    files = source_bytes(BASE_DIR / "data")
    async with web_client(importer, config) as (client, _app):
        response = await client.post("/api/v1/imports", files=multipart(files))
        assert response.status_code == 200, response.text
        result = response.json()
        assert not result["reused"] and result["active_run_id"] == result["run_id"]
        for start, end, values, _bookings, _unknown in EXPECTED:
            params = {"date_from": start.isoformat(), "date_to": end.isoformat()} if start else {}
            dashboard = (await client.get("/api/v1/dashboard", params=params)).json()
            assert dashboard["run_id"] == result["run_id"]
            for key, expected in zip(METRICS, values, strict=True):
                assert Decimal(dashboard["metrics"][key]["known_subtotal"]) == expected
        history = (await client.get("/api/v1/imports")).json()
        assert history["total"] == 1
        run = history["items"][0]
        assert run["actor"].startswith("web:") and run["actor"].endswith(":web-admin")
        for name, content in files.items():
            assert run["manifest"][name]["sha256"] == hashlib.sha256(content).hexdigest()
        repeat = (await client.post("/api/v1/imports", files=multipart(files))).json()
        assert repeat["reused"] and repeat["run_id"] == result["run_id"]
        assert (await client.get("/api/v1/imports")).json()["total"] == 1
        ch = await importer.analytics.helper.get_client()
        assert (
            await ch.query(
                "SELECT count(),uniqExact((source,business_key)) FROM financial_facts WHERE run_id={run:UUID}",
                parameters={"run": result["run_id"]},
            )
        ).result_rows == [(315, 315)]
    assert source_bytes(BASE_DIR / "data") == files


async def test_changed_upload_old_reuse_and_journaled_preflight(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Changed bytes replace the active view; older reuse and invalid CSV never roll it back."""
    importer, config = environment
    write_sources(tmp_path)
    original = source_bytes(tmp_path)
    changed = {
        **original,
        "reservations.csv": original["reservations.csv"].replace(b"1000", b"3000"),
    }
    async with web_client(importer, config) as (client, _app):
        first = (await client.post("/api/v1/imports", files=multipart(original))).json()
        second = (await client.post("/api/v1/imports", files=multipart(changed))).json()
        assert first["run_id"] != second["run_id"] and not second["reused"]
        older = (await client.post("/api/v1/imports", files=multipart(original))).json()
        assert older["reused"] and older["run_id"] == first["run_id"]
        assert older["active_run_id"] == second["run_id"]
        for files in (
            {"properties.csv": original["properties.csv"]},
            {**original, "expenses.csv": b"wrong,header\n"},
        ):
            failed = await client.post("/api/v1/imports", files=multipart(files))
            assert failed.status_code == 422
            assert failed.json()["outcome"] == "failed" and failed.json()["run_id"]
            detail = (await client.get("/api/v1/imports/" + failed.json()["run_id"])).json()["run"]
            assert detail["status"] == "failed" and detail["error_code"] == "preflight_failed"
        dashboard = (await client.get("/api/v1/dashboard")).json()
        assert dashboard["run_id"] == second["run_id"]
        assert Decimal(dashboard["metrics"]["gross"]["value"]) == 3000


async def test_upload_authorization_before_body_and_busy_lock(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Reject unauthenticated/viewer/foreign-origin requests before parsing and expose busy409."""
    importer, config = environment
    write_sources(tmp_path)
    files = source_bytes(tmp_path)
    viewer = await seed(importer.database, config, "web-viewer", "viewer", [])
    async with web_client(importer, config) as (client, _app):
        for origin in ("https://foreign.invalid", "null", ""):
            assert (
                await client.post(
                    "/api/v1/imports", content=b"not multipart", headers={"Origin": origin}
                )
            ).status_code == 403
        async with import_lock(importer.database):
            busy = await client.post("/api/v1/imports", files=multipart(files))
            assert busy.status_code == 409 and busy.json()["outcome"] == "busy"
        assert await attempts(importer) == []
        await client.post("/api/v1/auth/login", json={"username": "web-viewer", "password": viewer})
        assert (await client.post("/api/v1/imports", content=b"not multipart")).status_code == 403
        assert (await client.get("/imports")).status_code == 403
        await client.post("/api/v1/auth/logout")
        assert (await client.post("/api/v1/imports", content=b"not multipart")).status_code == 401
        assert await attempts(importer) == []


@pytest.mark.parametrize("failure", ["partial_ch", "before_commit", "after_commit", "unknown"])
async def test_web_failure_outcomes_use_real_publication_state(
    environment: tuple[ImportService, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Inject faults around actual CH writes and PG commits; the HTTP outcome must stay honest."""
    importer, config = environment
    write_sources(tmp_path)
    first = await importer.run(tmp_path, "initial")
    original = source_bytes(tmp_path)
    changed = {
        **original,
        "reservations.csv": original["reservations.csv"].replace(b"1000", b"3000"),
    }
    broken_config = config.db.model_copy(update={"host": "127.0.0.1", "port": 1})
    broken = DatabaseHelper(broken_config.dsn(), timeout=0.2)
    original_database = importer.database
    provider = Provider(scope=Scope.REQUEST)

    def pipeline() -> ImportService:
        """Use the isolated real importer at the typed Dishka publication boundary."""
        return importer

    provider.provide(pipeline, override=True)
    async with web_client(importer, config, providers=(provider,)) as (client, _app):
        try:
            with monkeypatch.context() as patch:
                if failure == "partial_ch":
                    ch = await importer.analytics.helper.get_client()
                    original_insert = ch.insert

                    async def partial(table: str, *args: Any, **kwargs: Any) -> Any:
                        """Persist raw rows, then lose the next table write in the isolated database."""
                        if table == "canonical_records":
                            raise ConnectionError("injected private transport context")
                        return await original_insert(table, *args, **kwargs)

                    patch.setattr(ch, "insert", partial)
                else:
                    original_publish = ImportRepo.publish

                    async def uncertain(repo: ImportRepo, *args: Any) -> None:
                        """Lose an acknowledgement before/after a genuine PG publication transaction."""
                        if failure != "before_commit":
                            await original_publish(repo, *args)
                        if failure == "unknown":
                            importer.database = broken
                        raise ConnectionError("injected private commit context")

                    patch.setattr(ImportRepo, "publish", uncertain)
                response = await client.post("/api/v1/imports", files=multipart(changed))
        finally:
            importer.database = original_database
            await broken.dispose()
        runs = await attempts(importer)
        second = runs[-1]
        committed = failure in {"after_commit", "unknown"}
        assert second.status == ("published" if committed else "failed")
        if failure == "after_commit":
            assert response.status_code == 200
        else:
            assert response.status_code == 503
            assert response.json()["outcome"] == ("unknown" if failure == "unknown" else "failed")
            assert response.json()["run_id"] == str(second.run_id)
        assert "private" not in response.text
        dashboard = (await client.get("/api/v1/dashboard")).json()
        assert dashboard["run_id"] == (str(second.run_id) if committed else first["run_id"])
        assert Decimal(dashboard["metrics"]["gross"]["value"]) == (3000 if committed else 1000)


async def test_cancelled_http_waiter_retains_staging_until_real_import_finishes(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel the HTTP waiter at a real write boundary without detaching work or deleting input early."""
    importer, config = environment
    write_sources(tmp_path)
    files = source_bytes(tmp_path)
    directories: list[Path] = []
    entered, release = asyncio.Event(), asyncio.Event()
    original_stage = ImportExecutionService.stage
    original_write = AnalyticsImportRepo.write

    def stage(directory: Path, files: dict[str, bytes]) -> None:
        """Observe the actual owned directory without replacing its writes."""
        directories.append(directory)
        original_stage(directory, files)

    async def paused(repo: AnalyticsImportRepo, *args: Any) -> None:
        """Pause while the real importer owns its PG lease and will write actual CH data."""
        entered.set()
        await release.wait()
        await original_write(repo, *args)

    monkeypatch.setattr(ImportExecutionService, "stage", staticmethod(stage))
    monkeypatch.setattr(AnalyticsImportRepo, "write", paused)
    async with web_client(importer, config) as (client, _app):
        running = asyncio.create_task(client.post("/api/v1/imports", files=multipart(files)))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
            assert directories and all(path.is_dir() for path in directories)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(running, 30)
        assert all(not path.exists() for path in directories)
        assert (await client.get("/api/v1/imports")).json()["items"][0]["status"] == "published"
        assert (await client.get("/api/v1/dashboard")).json()["state"] == "ready"
