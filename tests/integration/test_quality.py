"""Exercise scoped quality/history API and Jinja against actual PostgreSQL and ClickHouse."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from src.app.__main__ import create_app
from src.core.infra.exceptions import ImportFailed
from src.core.settings import BASE_DIR, Settings
from src.services.imports.service import ImportService
from tests.import_helpers import write_sources
from tests.integration.test_auth import seed
from tests.test_finance import fixture_rows

pytestmark = pytest.mark.integration


async def test_actual_quality_filters_provenance_counts_and_history_scope(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Verify observed cases, pagination, separate counters, UI and denial of global/foreign data."""
    importer, config = environment
    admin = await seed(importer.database, config, "quality-admin", "admin", [])
    viewer = await seed(importer.database, config, "quality-viewer", "viewer", ["V008"])
    empty = await seed(importer.database, config, "quality-empty", "viewer", [])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            assert (await client.get("/api/v1/quality-issues")).status_code == 401
            assert (await client.get("/quality")).headers["location"] == "/login"
            await client.post(
                "/api/v1/auth/login", json={"username": "quality-admin", "password": admin}
            )
            assert (await client.get("/api/v1/quality-issues")).json()["state"] == "no_snapshot"
            published = await importer.run(BASE_DIR / "data", "quality-test")
            assert published["summary"]["quality"]["by_code"]["OCCUPANCY_OVERLAP"] == 73
            response = await client.get("/api/v1/quality-issues")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["total"] == 222 and len(body["items"]) == 25
            counts = body["scope_counts"]
            assert (counts["issues"], counts["affected_rows"], counts["affected_entities"]) == (
                222,
                140,
                138,
            )
            assert counts["excluded_financial_entities"] == 6 and counts["discarded_raw_rows"] == 2
            overlap = (
                await client.get("/api/v1/quality-issues?code=OCCUPANCY_OVERLAP&limit=100")
            ).json()
            assert overlap["total"] == 73 and overlap["filtered_counts"]["affected_entities"] == 87
            assert len({i["property_id"] for i in overlap["items"]}) == 17
            assert len({i["issue_id"] for i in overlap["items"]}) == 73
            first = next(
                i
                for i in overlap["items"]
                if {i["business_key"], *[r["business_key"] for r in i["related_entities"]]}
                == {"R0033", "R0056"}
            )
            detail_url = "/api/v1/quality-issues/" + first["issue_id"] + "?run_id=" + body["run_id"]
            detail = await client.get(detail_url)
            assert detail.status_code == 200
            assert detail.json()["related"][0]["raw"][0]["values"]["reservation_id"] == "R0056"
            assert len(detail.json()["issue"]["effects"]["facts"]) == 2
            assert all(f["included"] for f in detail.json()["issue"]["effects"]["facts"])
            html = await client.get(
                "/quality/issues/" + first["issue_id"], params={"run_id": body["run_id"]}
            )
            assert (
                html.status_code == 200 and "Связанные записи" in html.text and "R0056" in html.text
            )
            raw_url = f"/api/v1/source-rows/{first['source']}/{first['row_number']}?run_id={body['run_id']}"
            assert (await client.get(raw_url)).json()["rows"][0]["row_number"] == first[
                "row_number"
            ]
            assert (await client.get("/api/v1/source-rows/reservations.csv/-1")).status_code == 422
            assert (
                await client.get("/api/v1/source-rows/reservations.csv/4294967296")
            ).status_code == 422
            assert (
                await client.get("/quality/raw/reservations.csv/" + str(first["row_number"]))
            ).status_code == 200
            assert (await client.get("/quality")).status_code == 200
            direct = (
                await client.get(
                    "/api/v1/quality-issues?code=DIRECT_WITH_OTA&source=reservations.csv&severity=warning&action=preserve_financial_decision&limit=100"
                )
            ).json()
            assert direct["total"] == 30
            pages = [
                (
                    await client.get(
                        "/api/v1/quality-issues",
                        params={"code": "OCCUPANCY_OVERLAP", "limit": 30, "offset": offset},
                    )
                ).json()
                for offset in (0, 30, 60)
            ]
            assert len({i["issue_id"] for p in pages for i in p["items"]}) == 73
            far = (
                await client.get("/api/v1/quality-issues?date_from=2028-01-01&date_to=2028-01-31")
            ).json()
            assert far["total"] == far["filtered_counts"]["snapshot_limitations"] == 6
            assert all(i["is_limitation"] for i in far["items"])
            assert (await client.get("/api/v1/quality-issues?limit=101")).status_code == 422
            assert (
                await client.get("/api/v1/quality-issues?date_from=2026-10-02&date_to=2026-10-01")
            ).status_code == 422
            assert (
                await client.get("/api/v1/quality-issues?run_id=" + str(uuid4()))
            ).status_code == 404
            with pytest.raises(ImportFailed):
                await importer.run(tmp_path / "absent", "failed-quality-test")
            history = (await client.get("/api/v1/imports")).json()
            assert history["total"] == 2 and history["active_run_id"] == published["run_id"]
            failure = next(r for r in history["items"] if r["status"] == "failed")
            assert (
                await client.get("/api/v1/quality-issues", params={"run_id": failure["run_id"]})
            ).status_code == 404
            assert (await client.get("/api/v1/imports?status=failed")).json()["total"] == 1
            assert (await client.get("/api/v1/imports/" + failure["run_id"])).json()[
                "comparison"
            ] is None
            assert (await client.get("/imports")).status_code == 200
            assert (await client.get("/imports/" + published["run_id"])).status_code == 200
            unknown = next(
                i
                for i in (
                    await client.get("/api/v1/quality-issues?section=limitations&limit=100")
                ).json()["items"]
                if i["property_id"] is None
            )
            await client.post(
                "/api/v1/auth/login", json={"username": "quality-viewer", "password": viewer}
            )
            scoped = (await client.get("/api/v1/quality-issues?limit=100")).json()
            assert all(i["property_id"] == "V008" for i in scoped["items"])
            assert scoped["scope_counts"]["issues"] < 222
            assert "V999" not in str(scoped) and "V777" not in str(scoped)
            assert (await client.get(detail_url)).status_code == 200
            assert (
                await client.get("/api/v1/quality-issues/" + unknown["issue_id"])
            ).status_code == 404
            assert (
                await client.get(
                    "/api/v1/source-rows/" + unknown["source"] + "/" + str(unknown["row_number"])
                )
            ).status_code == 404
            assert (await client.get("/api/v1/quality-issues?property_id=V001")).status_code == 403
            for url in (
                "/api/v1/imports",
                "/api/v1/imports/" + published["run_id"],
                "/imports",
                "/imports/" + published["run_id"],
            ):
                denied = await client.get(url)
                assert (
                    denied.status_code == 403
                    and "manifest" not in denied.text
                    and "failed-quality-test" not in denied.text
                )
            await client.post(
                "/api/v1/auth/login", json={"username": "quality-empty", "password": empty}
            )
            denied = (await client.get("/api/v1/quality-issues")).json()
            assert denied["total"] == denied["scope_counts"]["issues"] == 0
            assert denied["items"] == [] and denied["properties"] == []


async def test_history_compares_added_changed_absent_without_rewriting_snapshots(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Show immutable audit metadata and generic business-hash comparison on changed input."""
    importer, config = environment
    rows = fixture_rows(tmp_path)
    first = await importer.run(tmp_path, "first-operator")
    rows["reservations.csv"][0]["gross_booking_value"] = "2000"
    rows["expenses.csv"] = []
    rows["payments.csv"].append({**rows["payments.csv"][0], "payment_id": "payment-3"})
    write_sources(tmp_path, rows)
    second = await importer.run(tmp_path, "second-operator")
    assert (await importer.run(tmp_path, "repeat"))["reused"]
    password = await seed(importer.database, config, "history-admin", "admin", [])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "history-admin", "password": password}
            )
            data = (await client.get("/api/v1/imports/" + second["run_id"])).json()
            assert data["base_run_id"] == first["run_id"]
            assert data["comparison"]["reservations.csv"]["changed"] == ["booking-1"]
            assert data["comparison"]["expenses.csv"]["absent_in_new_snapshot"] == ["expense-1"]
            assert data["comparison"]["payments.csv"]["added"] == ["payment-3"]
            assert "не доказывает удаление" in data["notice"]
            explicit = await client.get(
                "/api/v1/imports/" + second["run_id"], params={"base_run_id": first["run_id"]}
            )
            assert explicit.json()["comparison"] == data["comparison"]
            assert (
                await client.get(
                    "/api/v1/imports/" + second["run_id"], params={"base_run_id": str(uuid4())}
                )
            ).status_code == 404
            old = (await client.get("/api/v1/imports/" + first["run_id"])).json()
            assert (
                old["run"]["actor"] == "first-operator"
                and old["run"]["manifest"] != data["run"]["manifest"]
            )
            assert (await client.get("/api/v1/imports?limit=1&offset=1")).json()["items"][0][
                "run_id"
            ] == first["run_id"]
            html = await client.get("/imports/" + second["run_id"])
            assert html.status_code == 200 and "Отсутствует в новом снимке" in html.text


async def test_quality_historical_foreign_rows_and_related_payments_are_hidden(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Source history and cross-object references cannot leak through diagnostic effects/counters."""
    importer, config = environment
    rows = fixture_rows(tmp_path)
    rows["properties.csv"].append({**rows["properties.csv"][0], "property_id": "foreign"})
    original = {
        **rows["reservations.csv"][0],
        "property_id": "foreign",
        "gross_booking_value": "999999",
    }
    rows["reservations.csv"][0]["updated_at"] = "2026-09-02"
    rows["reservations.csv"].insert(0, original)
    rows["reservations.csv"].append({**original, "reservation_id": "foreign-booking"})
    rows["payments.csv"][0]["reservation_id"] = "foreign-booking"
    write_sources(tmp_path, rows)
    await importer.run(tmp_path, "test")
    password = await seed(importer.database, config, "scoped-viewer", "viewer", ["villa-1"])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "scoped-viewer", "password": password}
            )
            data = (await client.get("/api/v1/quality-issues?limit=100")).json()
            assert "999999" not in str(data)
            for issue in data["items"]:
                assert all(
                    entity["property_id"] == "villa-1" for entity in issue["related_entities"]
                )
                assert all(
                    fact["property_id"] == "villa-1" for fact in issue["effects"].get("facts", [])
                )
                detail = await client.get("/api/v1/quality-issues/" + issue["issue_id"])
                assert detail.status_code == 200 and "999999" not in detail.text
            assert (await client.get("/api/v1/source-rows/reservations.csv/2")).status_code == 404


@pytest.mark.parametrize("invalid_checkout", ["invalid", "1800-01-03", "2300-01-03"])
async def test_rule_and_undated_limitations_survive_period_filter(
    environment: tuple[ImportService, Settings], tmp_path: Path, invalid_checkout: str
) -> None:
    """Synthetic malformed dates and rule overlap remain explicit snapshot limitations."""
    importer, config = environment
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0]["check_out"] = invalid_checkout
    rows["commission_rules.csv"].append(
        {**rows["commission_rules.csv"][0], "effective_from": "2026-09-01"}
    )
    write_sources(tmp_path, rows)
    await importer.run(tmp_path, "test")
    password = await seed(importer.database, config, "limits-viewer", "viewer", ["villa-1"])
    app = create_app(config)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            await client.post(
                "/api/v1/auth/login", json={"username": "limits-viewer", "password": password}
            )
            data = (
                await client.get(
                    "/api/v1/quality-issues?date_from=2028-01-01&section=limitations&limit=100"
                )
            ).json()
            assert "COMMISSION_RULE_OVERLAP" in data["filtered_counts"]["by_code"]
            assert any(
                i["source"] == "reservations.csv" and i["period_start"] is None
                for i in data["items"]
            )
            assert data["filtered_counts"]["snapshot_limitations"] == data["total"]
            assert (
                await client.get("/quality?section=limitations&date_from=2028-01-01")
            ).status_code == 200
