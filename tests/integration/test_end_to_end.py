"""Verify that real cross-database failures remain safe when read through the app."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import pytest
from sqlalchemy import update

from src.analytics.quality import QualityRepo
from src.app.__main__ import create_app
from src.core.infra.exceptions import ImportBusy, ImportFailed, PublicationUnknown
from src.core.settings import Settings
from src.database.helper import DatabaseHelper
from src.database.models import UserSession
from src.database.repositories.imports import ImportRepo
from src.services.imports.records import Issue
from src.services.imports.service import ImportService
from tests.import_helpers import write_sources
from tests.integration.test_auth import seed
from tests.integration.test_imports import attempts
from tests.test_finance import fixture_rows

pytestmark = pytest.mark.integration


class Navigation(HTMLParser):
    """Read successful input defaults and destination links from actual rendered HTML."""

    def __init__(self, html: str) -> None:
        """Parse the response without assuming where a snapshot field is implemented."""
        super().__init__()
        self.inputs: dict[str, str] = {}
        self.links: list[str] = []
        self.filter_links: list[str] = []
        self.in_filter = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Collect named nonempty input values and normal anchor destinations."""
        attributes = dict(attrs)
        if tag == "form":
            self.in_filter = attributes.get("id") == "finance-filter"
        if self.in_filter and tag == "input" and attributes.get("name") and attributes.get("value"):
            self.inputs[attributes["name"]] = attributes["value"]
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
            if self.in_filter:
                self.filter_links.append(attributes["href"])

    def handle_endtag(self, tag: str) -> None:
        """Stop collecting filter controls at the form boundary."""
        if tag == "form":
            self.in_filter = False


@asynccontextmanager
async def application_client(
    service: ImportService, config: Settings
) -> AsyncIterator[httpx.AsyncClient]:
    """Log into an actual app with independent PG/CH helpers and a random password."""
    password = await seed(service.database, config, "e06-admin", "admin", [])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            response = await client.post(
                "/api/v1/auth/login", json={"username": "e06-admin", "password": password}
            )
            assert response.status_code == 200
            yield client


async def assert_visible(client: httpx.AsyncClient, run_id: str, gross: str) -> None:
    """Check dashboard, details, quality and HTML all resolve the same complete snapshot."""
    dashboard = await client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["run_id"] == run_id and body["metrics"]["gross"]["value"] == gross
    assert len(body["records"]) == 4
    detail = await client.get("/api/v1/reservations/booking-1")
    assert detail.json()["run_id"] == run_id
    assert detail.json()["fact"]["gross"] == gross
    quality = await client.get("/api/v1/quality-issues")
    assert quality.status_code == 200 and quality.json()["run_id"] == run_id
    for route in ("/dashboard", "/quality", "/detail/reservations/booking-1"):
        html = await client.get(route)
        assert html.status_code == 200 and run_id in html.text


@pytest.mark.parametrize("failure", ["partial_ch", "before_commit", "after_commit", "unknown"])
async def test_failure_recovery_is_visible_through_application(
    environment: tuple[ImportService, Settings],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Inject boundary failures around real writes/commit and read the durable result via HTTP.

    Partial CH writes persist only raw_rows. Lost acknowledgements surround the real PG
    commit; the unknown case runs actual reconciliation against a closed TCP endpoint.
    All data and failures belong to disposable databases, never the working snapshot.
    """
    service, config = environment
    write_sources(tmp_path)
    first = await service.run(tmp_path, "e06-test")
    client_ch = await service.analytics.helper.get_client()
    path = tmp_path / "reservations.csv"
    path.write_bytes(path.read_bytes().replace(b"1000", b"3000"))
    database = service.database
    unavailable = config.db.model_copy(update={"host": "127.0.0.1", "port": 1})
    broken = DatabaseHelper(unavailable.dsn(), timeout=0.2)
    async with application_client(service, config) as client:
        await assert_visible(client, first["run_id"], "1000.00")
        try:
            with monkeypatch.context() as patch:
                if failure == "partial_ch":
                    original_insert = client_ch.insert

                    async def partial(table: str, *args: Any, **kwargs: Any) -> Any:
                        """Fail the second table after the first real insert has completed."""
                        if table == "canonical_records":
                            raise ConnectionError("Injected failure after durable raw_rows insert")
                        return await original_insert(table, *args, **kwargs)

                    patch.setattr(client_ch, "insert", partial)
                else:
                    original_publish = ImportRepo.publish

                    async def uncertain(repo: ImportRepo, *args: Any) -> None:
                        """Simulate acknowledgement loss around a real atomic publication."""
                        if failure != "before_commit":
                            await original_publish(repo, *args)
                        if failure == "unknown":
                            service.database = broken
                        raise ConnectionError("Injected publication acknowledgement loss")

                    patch.setattr(ImportRepo, "publish", uncertain)
                if failure == "after_commit":
                    result = await service.run(tmp_path, "e06-test")
                    assert not result["reused"]
                else:
                    expected_error = PublicationUnknown if failure == "unknown" else ImportFailed
                    with pytest.raises(expected_error):
                        await service.run(tmp_path, "e06-test")
        finally:
            service.database = database
            await broken.dispose()
        runs = await attempts(service)
        assert len(runs) == 2
        second = runs[-1]
        committed = failure in {"after_commit", "unknown"}
        assert second.status == ("published" if committed else "failed")
        await assert_visible(
            client,
            str(second.run_id) if committed else first["run_id"],
            "3000.00" if committed else "1000.00",
        )
        if failure == "partial_ch":
            for table, count in (
                ("raw_rows", 6),
                ("canonical_records", 0),
                ("quality_issues", 0),
                ("financial_facts", 0),
            ):
                actual = await client_ch.query(
                    f"SELECT count() FROM {table} WHERE run_id={{id:UUID}}",
                    parameters={"id": second.run_id},
                )
                assert actual.result_rows == [(count,)]
            assert (
                await client.get("/api/v1/dashboard", params={"run_id": str(second.run_id)})
            ).status_code == 404
        retry = await service.run(tmp_path, "e06-retry")
        assert retry["reused"] == committed
        assert (retry["run_id"] == str(second.run_id)) == committed
        await assert_visible(client, retry["run_id"], "3000.00")
        unchanged_quality = (await client.get("/api/v1/quality-issues")).json()
        repeat = await service.run(tmp_path, "e06-repeat")
        assert repeat["reused"] and repeat["run_id"] == retry["run_id"]
        assert (await client.get("/api/v1/quality-issues")).json() == unchanged_quality
        assert len(await attempts(service)) == (2 if committed else 3)
        totals = await client_ch.query(
            "SELECT count(), uniqExact((source,business_key)) FROM financial_facts "
            "WHERE run_id={id:UUID}",
            parameters={"id": UUID(retry["run_id"])},
        )
        assert totals.result_rows == [(4, 4)]
        historical = (
            await client.get("/api/v1/dashboard", params={"run_id": first["run_id"]})
        ).json()
        assert historical["metrics"]["gross"]["value"] == "1000.00"


async def test_readers_see_old_snapshot_during_concurrent_import(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real separate PG connections serialize imports while HTTP readers retain a complete run."""
    service, config = environment
    write_sources(tmp_path)
    first = await service.run(tmp_path, "e06-initial")
    path = tmp_path / "reservations.csv"
    path.write_bytes(path.read_bytes().replace(b"1000", b"3000"))
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.analytics.write

    async def paused(*args: Any) -> None:
        """Hold the actual PG lease at the boundary before writing CH tables."""
        entered.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(service.analytics, "write", paused)
    second_database = DatabaseHelper(config.db.dsn())
    try:
        async with application_client(service, config) as client:
            running = asyncio.create_task(service.run(tmp_path, "e06-first"))
            try:
                await asyncio.wait_for(entered.wait(), 10)
                with pytest.raises(ImportBusy):
                    await ImportService(second_database, service.analytics).run(
                        tmp_path, "e06-second"
                    )
                await assert_visible(client, first["run_id"], "1000.00")
            finally:
                release.set()
                result = await asyncio.wait_for(running, 20)
            await assert_visible(client, result["run_id"], "3000.00")
            assert len(await attempts(service)) == 2
    finally:
        await second_database.dispose()


async def test_expired_session_is_rejected_across_all_screens(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Expire a real persisted session and reject every financial/quality/audit entry point."""
    service, config = environment
    write_sources(tmp_path)
    run = await service.run(tmp_path, "e06-test")
    async with application_client(service, config) as client:
        async with service.database.session_factory() as session:
            await session.execute(
                update(UserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
        for route in (
            "/api/v1/dashboard",
            "/api/v1/properties",
            "/api/v1/reservations/booking-1",
            "/api/v1/financial-records/payments/payment-1",
            "/api/v1/quality-issues",
            "/api/v1/source-rows/reservations/2",
            "/api/v1/imports",
            "/api/v1/imports/" + run["run_id"],
        ):
            assert (await client.get(route)).status_code == 401, route
        for route in (
            "/dashboard",
            "/detail/reservations/booking-1",
            "/quality",
            "/quality/raw/reservations/2",
            "/imports",
            "/imports/" + run["run_id"],
        ):
            response = await client.get(route)
            assert response.status_code == 303 and response.headers["location"] == "/login", route


async def test_rendered_navigation_retains_snapshot_after_publication(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Submit old HTML controls after publication and retain old money/quality on every link."""
    service, config = environment
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0]["channel"] = "Direct"
    write_sources(tmp_path, rows)
    first = await service.run(tmp_path, "e06-initial")
    async with application_client(service, config) as client:
        dashboard = Navigation((await client.get("/dashboard")).text)
        quality = Navigation((await client.get("/quality?limit=1")).text)
        rows["reservations.csv"][0]["gross_booking_value"] = "3000"
        write_sources(tmp_path, rows)
        second = await service.run(tmp_path, "e06-changed")
        assert (await client.get("/api/v1/dashboard")).json()["run_id"] == second["run_id"]
        submitted = await client.get("/dashboard", params=dashboard.inputs)
        assert f"<code>{first['run_id']}</code>" in submitted.text
        reset = next(link for link in dashboard.filter_links if link.startswith("/dashboard"))
        assert f"<code>{first['run_id']}</code>" in (await client.get(reset)).text
        next_page = next(
            link for link in quality.links if parse_qs(urlsplit(link).query).get("offset") == ["1"]
        )
        limitations = next(
            link
            for link in quality.links
            if parse_qs(urlsplit(link).query).get("section") == ["limitations"]
        )
        for link in (next_page, limitations):
            response = await client.get(link)
            assert response.status_code == 200
            assert f"<code>{first['run_id']}</code>" in response.text
        previous_page = next(
            link
            for link in Navigation((await client.get(next_page)).text).links
            if parse_qs(urlsplit(link).query).get("offset") == ["0"]
        )
        assert f"<code>{first['run_id']}</code>" in (await client.get(previous_page)).text


async def test_quality_response_pins_run_during_publication(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish during a real quality read; items, effects and counters remain from one run."""
    service, config = environment
    write_sources(tmp_path)
    first = await service.run(tmp_path, "e06-initial")
    original = QualityRepo.issues
    switched = False

    async def publish_after_resolve(
        repo: QualityRepo, run_id: UUID, ids: tuple[str, ...] | None
    ) -> list[tuple[str, Issue]]:
        """Change the entire input after run resolution but before the actual CH query."""
        nonlocal switched
        if not switched:
            switched = True
            rows = fixture_rows(tmp_path)
            rows["reservations.csv"][0]["channel"] = "Direct"
            rows["reservations.csv"][0]["gross_booking_value"] = "3000"
            write_sources(tmp_path, rows)
            await service.run(tmp_path, "e06-concurrent")
        return await original(repo, run_id, ids)

    async with application_client(service, config) as client:
        initial = (await client.get("/api/v1/quality-issues")).json()
        monkeypatch.setattr(QualityRepo, "issues", publish_after_resolve)
        concurrent = (await client.get("/api/v1/quality-issues")).json()
        assert concurrent == initial and concurrent["run_id"] == first["run_id"]
        subsequent = (await client.get("/api/v1/quality-issues")).json()
        assert subsequent["run_id"] != first["run_id"]
        assert subsequent["total"] == initial["total"] + 1
        assert all(item["run_id"] == subsequent["run_id"] for item in subsequent["items"])
