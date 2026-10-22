"""Verify the financial vertical using actual PG/CH migrations, SQL, auth and HTML."""

from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from src.analytics.dashboard import DashboardRepo
from src.app.__main__ import create_app
from src.core.infra.exceptions import ImportFailed
from src.core.settings import BASE_DIR, Settings
from src.database.models import ImportRun
from src.database.repositories.imports import ImportRepo
from src.finance.policy import CALCULATION_VERSION
from src.services.imports.service import ImportService
from tests.import_helpers import write_sources
from tests.integration.test_auth import seed
from tests.test_finance import EXPECTED, METRICS, fixture_rows

pytestmark = pytest.mark.integration


async def test_real_financial_publication_controls_and_http_scope(
    environment: tuple[ImportService, Settings],
) -> None:
    """Publish a new calculated snapshot, keep E03 immutable, and verify dates/scopes in real SQL."""
    importer, config = environment
    password = await seed(importer.database, config, "finance-admin", "admin", [])
    viewer_password = await seed(importer.database, config, "finance-viewer", "viewer", ["V019"])
    empty_password = await seed(importer.database, config, "finance-empty", "viewer", [])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            assert (await client.get("/api/v1/dashboard")).status_code == 401
            assert (await client.get("/dashboard")).headers["location"] == "/login"
            await client.post(
                "/api/v1/auth/login", json={"username": "finance-admin", "password": password}
            )
            assert (await client.get("/api/v1/dashboard")).json()["state"] == "no_snapshot"
            assert "пока не готовы" in (await client.get("/dashboard")).text
            canonical = ImportService(
                importer.database, importer.analytics, "canonical-v2", calculation_version=None
            )
            old = await canonical.run(BASE_DIR / "data", "e04-test")
            assert (await client.get("/api/v1/dashboard")).json()["state"] == "not_calculated"
            current = await importer.run(BASE_DIR / "data", "e04-test")
            repeat = await importer.run(BASE_DIR / "data", "e04-test")
            assert (
                current["run_id"] != old["run_id"]
                and repeat["run_id"] == current["run_id"]
                and repeat["reused"]
            )
            ch = await importer.analytics.helper.get_client()
            assert (
                await ch.query(
                    "SELECT count(),uniqExact((source,business_key)) FROM financial_facts WHERE run_id={id:UUID}",
                    parameters={"id": current["run_id"]},
                )
            ).result_rows == [(315, 315)]
            assert (
                await ch.query(
                    "SELECT count() FROM financial_facts WHERE run_id={id:UUID}",
                    parameters={"id": old["run_id"]},
                )
            ).result_rows == [(0,)]
            async with importer.database.session_factory() as session:
                previous = await session.get(ImportRun, UUID(old["run_id"]))
                assert previous.calculation_version is None and previous.status == "published"
            for start, end, expected, admitted, unknown in EXPECTED:
                params = (
                    {"date_from": start.isoformat(), "date_to": end.isoformat()} if start else {}
                )
                response = await client.get("/api/v1/dashboard", params=params)
                assert response.status_code == 200, response.text
                body = response.json()
                assert [Decimal(body["metrics"][name]["known_subtotal"]) for name in METRICS] == [
                    Decimal(n) for n in expected
                ]
                assert body["coverage"]["reservations.csv"]["included"] == admitted
                assert body["unknown_fees"] == unknown
                for series in body["charts"].values():
                    for key in METRICS:
                        assert sum(
                            Decimal(point["metrics"][key]["known_subtotal"])
                            for point in series["points"]
                        ) == Decimal(body["metrics"][key]["known_subtotal"])
                assert (
                    body["run_id"] == current["run_id"]
                    and body["calculation_version"] == CALCULATION_VERSION
                )
                if start is None:
                    assert (body["date_from"], body["date_to"]) == ("2026-09-01", "2026-11-05")
                    assert body["metrics"]["management"]["value"] is None
                    assert any(
                        row["observed_amount"] == "12727390.00" for row in body["unallocated"]
                    )
                    records = {
                        record["business_key"]: record
                        for group in body["unallocated"]
                        for record in group["records"]
                    }
                    assert set(records) == {"E0017", "P0017", "R0034"}
                    assert records["E0017"]["fact_date"] == "2026-10-21"
                    assert Decimal(records["E0017"]["amount"]) == 834779
                    assert Decimal(records["P0017"]["amount"]) == 12727390
                    assert Decimal(records["R0034"]["gross"]) == 14462943
                    assert all(
                        not record["included"]
                        and "UNKNOWN_PROPERTY" in record["reasons"]
                        and record["source_rows"]
                        for record in records.values()
                    )
                    assert all(
                        group["count"] == len(group["records"]) for group in body["unallocated"]
                    )
                for group in body["unallocated"]:
                    assert all(
                        record["source"] == group["source"]
                        and record["currency"] == group["currency"]
                        and body["date_from"] <= record["fact_date"] <= body["date_to"]
                        for record in group["records"]
                    )
            assert (
                await client.get(
                    "/api/v1/dashboard", params={"date_from": "2026-10-02", "date_to": "2026-10-01"}
                )
            ).status_code == 422
            assert (await client.get("/api/v1/dashboard?date_from=bad")).status_code == 422
            assert (
                await client.get("/api/v1/dashboard", params={"run_id": str(uuid4())})
            ).status_code == 404
            assert (await client.get("/api/v1/dashboard", params={"run_id": old["run_id"]})).json()[
                "state"
            ] == "not_calculated"
            detail = (
                await client.get("/api/v1/reservations/R0002", params={"run_id": current["run_id"]})
            ).json()
            assert Decimal(detail["fact"]["management"]) == 2002396
            assert detail["commission_rules"][0]["values"]["management_fee_rate"] == "0.17"
            assert detail["raw"][0]["row_number"] == 3
            html = await client.get(
                "/detail/reservations/R0002", params={"run_id": current["run_id"]}
            )
            assert (
                html.status_code == 200
                and "ROUND_HALF_UP" in html.text
                and "Исходные строки" in html.text
            )
            dashboard_html = await client.get("/dashboard")
            assert dashboard_html.status_code == 200
            unallocated_html = dashboard_html.text.split('id="unallocated"', 1)[1].split(
                "</section>", 1
            )[0]
            assert "Стоимость брони · Gross" in unallocated_html
            for source, key in (
                ("expenses", "E0017"),
                ("payments", "P0017"),
                ("reservations", "R0034"),
            ):
                assert f"/detail/{source}/{key}?run_id={current['run_id']}" in unallocated_html
                response = await client.get(
                    f"/detail/{source}/{key}", params={"run_id": current["run_id"]}
                )
                assert response.status_code == 200 and "Исходные строки" in response.text
            assert (await client.get("/api/v1/dashboard?property_id=V019")).json()[
                "unallocated"
            ] == []
            await client.post(
                "/api/v1/auth/login",
                json={"username": "finance-viewer", "password": viewer_password},
            )
            scoped = (await client.get("/api/v1/dashboard")).json()
            assert scoped["scope"] == ["V019"] and scoped["unallocated"] == []
            assert 'id="unallocated"' not in (await client.get("/dashboard")).text
            for source, row in (
                ("expenses.csv", 18),
                ("payments.csv", 18),
                ("reservations.csv", 35),
            ):
                assert (await client.get(f"/quality/raw/{source}/{row}")).status_code == 404
            assert all(row["property_id"] == "V019" for row in scoped["records"])
            for series in scoped["charts"].values():
                for key in METRICS:
                    assert sum(
                        Decimal(point["metrics"][key]["known_subtotal"])
                        for point in series["points"]
                    ) == Decimal(scoped["metrics"][key]["known_subtotal"])
            assert len((await client.get("/api/v1/properties")).json()) == 1
            assert (await client.get("/api/v1/dashboard?property_id=V001")).status_code == 403
            assert (await client.get("/api/v1/reservations/R0001")).status_code == 404
            assert (await client.get("/api/v1/financial-records/payments/P0017")).status_code == 404
            assert (await client.get("/detail/reservations/R0001")).status_code == 404
            assert (
                await client.get(
                    "/api/v1/dashboard?run_id=" + current["run_id"] + "&property_id=V001"
                )
            ).status_code == 403
            await client.post(
                "/api/v1/auth/login", json={"username": "finance-empty", "password": empty_password}
            )
            empty = (await client.get("/api/v1/dashboard")).json()
            assert empty["state"] == "empty" and empty["records"] == [] and empty["scope"] == []
            assert Decimal(empty["metrics"]["owner_estimate"]["value"]) == 0
            assert empty["metrics"]["management"]["is_complete"]
            assert empty["charts"] is None
            assert 'id="dashboard-chart-data"' not in (await client.get("/dashboard")).text


async def test_historical_raw_is_scoped_and_missing_ids_remain_excluded(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """A latest-property viewer cannot read a foreign older version; malformed money is covered."""
    importer, config = environment
    rows = fixture_rows(tmp_path)
    rows["properties.csv"].append({**rows["properties.csv"][0], "property_id": "villa-2"})
    old = {**rows["reservations.csv"][0], "property_id": "villa-2", "gross_booking_value": "999999"}
    rows["reservations.csv"][0]["updated_at"] = "2026-09-02"
    rows["reservations.csv"].insert(0, old)
    rows["expenses.csv"][0]["expense_id"] = "   "
    rows["payments.csv"][1]["property_id"] = "villa-2"
    write_sources(tmp_path, rows)
    published = await importer.run(tmp_path, "test")
    password = await seed(importer.database, config, "history-viewer", "viewer", ["villa-1"])
    admin_password = await seed(importer.database, config, "history-admin", "admin", [])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "history-viewer", "password": password}
            )
            response = await client.get("/api/v1/reservations/booking-1")
            assert (
                response.status_code == 200
                and "villa-2" not in response.text
                and "999999" not in response.text
            )
            assert len(response.json()["raw"]) == 1
            data = (await client.get("/api/v1/dashboard")).json()
            assert data["coverage"]["expenses.csv"]["excluded"] == 1
            assert not data["metrics"]["expenses"]["is_complete"]
            broken = next(row for row in data["records"] if row["source"] == "expenses.csv")
            assert not broken["included"]
            raw = await client.get("/api/v1/financial-records/expenses/" + broken["business_key"])
            assert raw.status_code == 200 and raw.json()["raw"][0]["values"]["amount"] == "20"
            await client.post(
                "/api/v1/auth/login", json={"username": "history-admin", "password": admin_password}
            )
            data = (await client.get("/api/v1/dashboard")).json()
            assert any(
                row["source"] == "payments.csv" and row["count"] == 1 for row in data["unallocated"]
            )
            mismatch = next(row for row in data["unallocated"] if row["source"] == "payments.csv")
            assert "PROPERTY_MISMATCH" in mismatch["records"][0]["reasons"]
            assert not mismatch["records"][0]["included"]
            assert data["run_id"] == published["run_id"]


async def test_single_response_pins_run_during_concurrent_publication(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new active pointer midway through a read never changes that response's data or scope."""
    importer, config = environment
    rows = fixture_rows(tmp_path)
    first = await importer.run(tmp_path, "test")
    password = await seed(importer.database, config, "pin-admin", "admin", [])
    app = create_app(config)
    original = DashboardRepo.facts
    changed = False

    async def switch_after_resolve(
        repo: DashboardRepo, run_id: UUID, *args: object, **kwargs: object
    ) -> list:
        """Publish changed bytes after the service already resolved its permitted run."""
        nonlocal changed
        if not changed:
            changed = True
            rows["reservations.csv"][0]["gross_booking_value"] = "2000"
            write_sources(tmp_path, rows)
            await importer.run(tmp_path, "other-operator")
        return await original(repo, run_id, *args, **kwargs)

    monkeypatch.setattr(DashboardRepo, "facts", switch_after_resolve)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "pin-admin", "password": password}
            )
            body = (await client.get("/api/v1/dashboard")).json()
            assert body["run_id"] == first["run_id"]
            assert Decimal(body["metrics"]["gross"]["value"]) == 1000
            assert (
                sum(
                    Decimal(point["metrics"]["gross"]["value"])
                    for point in body["charts"]["trend"]["points"]
                )
                == 1000
            )
            next_body = (await client.get("/api/v1/dashboard")).json()
            assert (
                next_body["run_id"] != first["run_id"]
                and Decimal(next_body["metrics"]["gross"]["value"]) == 2000
            )
            assert (
                sum(
                    Decimal(point["metrics"]["gross"]["value"])
                    for point in next_body["charts"]["trend"]["points"]
                )
                == 2000
            )


async def test_real_clickhouse_failure_is_unavailable_not_zero(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Use an actual refused TCP connection; neither HTML nor API substitutes financial zeros."""
    importer, config = environment
    write_sources(tmp_path)
    await importer.run(tmp_path, "test")
    password = await seed(importer.database, config, "offline-admin", "admin", [])
    broken_config = config.model_copy(deep=True)
    broken_config.clickhouse.port = 1
    broken_config.clickhouse.timeout = 1
    app = create_app(broken_config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "offline-admin", "password": password}
            )
            api = await client.get("/api/v1/dashboard")
            assert api.status_code == 503 and "metrics" not in api.json()
            html = await client.get("/dashboard")
            assert html.status_code == 503 and "Финансовые данные недоступны" in html.text
            assert (await client.get("/api/v1/quality-issues")).status_code == 503
            quality_html = await client.get("/quality")
            assert quality_html.status_code == 503 and "Данные недоступны" in quality_html.text
            assert config.clickhouse.password not in html.text or not config.clickhouse.password


async def test_real_pg_failure_before_auth_does_not_become_financial_zero(
    environment: tuple[ImportService, Settings],
) -> None:
    """Refuse the actual authentication DB connection and return explicit 503 to HTML/API."""
    importer, config = environment
    password = await seed(importer.database, config, "pg-offline-admin", "admin", [])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "pg-offline-admin", "password": password}
            )
            broken = config.model_copy(deep=True)
            broken.db.port = 1
            broken.db.timeout = 1
            unavailable = create_app(broken)
            async with unavailable.router.lifespan_context(unavailable):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=unavailable),
                    base_url=config.auth.origin,
                    cookies=client.cookies,
                ) as offline:
                    api = await offline.get("/api/v1/dashboard")
                    html = await offline.get("/dashboard")
                    assert api.status_code == 503 and "metrics" not in api.json()
                    assert html.status_code == 503 and "Финансовые данные недоступны" in html.text
                    assert (await offline.get("/api/v1/quality-issues")).status_code == 503
                    assert (await offline.get("/api/v1/imports")).status_code == 503


async def test_duplicate_financial_write_cannot_switch_active(
    environment: tuple[ImportService, Settings], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Physically duplicate financial rows in CH and preserve the previous published run."""
    importer, _ = environment
    rows = fixture_rows(tmp_path)
    first = await importer.run(tmp_path, "test")
    rows["reservations.csv"][0]["gross_booking_value"] = "2000"
    write_sources(tmp_path, rows)
    original = importer.analytics.write

    async def duplicate(run_id: UUID, snapshot: object) -> None:
        """Inject a real duplicate insertion after all normal CH writes acknowledged."""
        await original(run_id, snapshot)
        client = await importer.analytics.helper.get_client()
        await client.command(
            "INSERT INTO financial_facts SELECT * FROM financial_facts WHERE run_id={run:UUID}",
            parameters={"run": run_id},
        )

    with monkeypatch.context() as patch:
        patch.setattr(importer.analytics, "write", duplicate)
        with pytest.raises(ImportFailed, match="SnapshotMismatch"):
            await importer.run(tmp_path, "test")
    async with importer.database.session_factory() as session:
        assert str(await ImportRepo(session).active_id()) == first["run_id"]
    successful = await importer.run(tmp_path, "test")
    assert successful["run_id"] != first["run_id"]
    assert Decimal(successful["summary"]["financial"]["metrics"]["gross"]["value"]) == 2000
