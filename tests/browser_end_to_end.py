"""Run the complete E06 Chrome/CLI flow against disposable real PostgreSQL/ClickHouse."""

import asyncio
import csv
import json
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.async_api import Page, async_playwright

from src.core.settings import BASE_DIR, Settings
from tests.integration.test_auth import seed
from tests.real_environment import disposable_databases, migrated_environment
from tests.test_finance import EXPECTED, METRICS
from web.templating import money


def process_environment(config: Settings) -> dict[str, str]:
    """Pass isolated database settings to child CLI/server processes without logging secrets."""
    environment = os.environ.copy()
    environment.update(
        DB_NAME=config.db.database,
        DB_HOST=config.db.host,
        DB_PORT=str(config.db.port),
        DB_USER=config.db.user,
        DB_PASSWORD=config.db.password,
        CH_DATABASE=config.clickhouse.database,
        CH_HOST=config.clickhouse.host,
        CH_PORT=str(config.clickhouse.port),
        CH_USER=config.clickhouse.user,
        CH_PASSWORD=config.clickhouse.password,
        CH_SECURE=str(config.clickhouse.secure).lower(),
        AUTH_ORIGIN=config.auth.origin,
        APP_DEBUG="false",
        BROKER_ENABLED="false",
        PYTHONIOENCODING="utf-8",
    )
    return environment


async def import_cli(config: Settings, directory: Path) -> dict:
    """Import through the actual operator entry point and require a successful JSON result."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.cli.import_data",
        "--data-dir",
        str(directory),
        "--actor",
        "e06-browser",
        cwd=BASE_DIR,
        env=process_environment(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _errors = await asyncio.wait_for(process.communicate(), 45)
        assert process.returncode == 0, "Isolated CLI import failed"
        return json.loads(output)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@asynccontextmanager
async def live_application(config: Settings) -> AsyncIterator[str]:
    """Own a real local HTTP server using only the disposable databases for this run."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config.auth.origin = f"http://127.0.0.1:{port}"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "src.app.__main__:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
        "--log-level",
        "warning",
        cwd=BASE_DIR,
        env=process_environment(config),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        async with asyncio.timeout(30):
            async with httpx.AsyncClient(timeout=2) as client:
                while True:
                    assert process.returncode is None, "Isolated Uvicorn exited before readiness"
                    try:
                        if (
                            await client.get(config.auth.origin + "/health/ready")
                        ).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
        yield config.auth.origin
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


async def login(page: Page, origin: str, username: str, password: str) -> None:
    """Enter through the rendered form and require the authenticated dashboard redirect."""
    await page.goto(origin + "/dashboard")
    await page.wait_for_url("**/login")
    await page.get_by_label("Логин").fill(username)
    await page.get_by_label("Пароль").fill(password)
    await page.get_by_role("button", name="Войти", exact=True).click()
    await page.wait_for_url("**/dashboard")


async def assert_ui_api(page: Page, origin: str, expected: list[int] | None = None) -> dict:
    """Compare every displayed metric with its server API value, including unknown fees."""
    query = urlsplit(page.url).query
    response = await page.request.get(origin + "/api/v1/dashboard?" + query)
    assert response.status == 200
    data = await response.json()
    assert data["run_id"] in await page.locator(".metadata-details").text_content()
    for index, key in enumerate(METRICS):
        metric = data["metrics"][key]
        value = metric["known_subtotal"] if key == "owner_estimate" else metric["value"]
        assert await page.locator(f"[data-metric={key}] strong").text_content() == money(value)
        if expected is not None:
            assert Decimal(metric["known_subtotal"]) == Decimal(expected[index])
    return data


def changed_copy(directory: Path) -> None:
    """Copy all five source files and change one valid gross only in the temporary copy."""
    for path in (BASE_DIR / "data").glob("*.csv"):
        shutil.copyfile(path, directory / path.name)
    path = directory / "reservations.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    for row in rows:
        if row["reservation_id"] == "R0002":
            row["gross_booking_value"] = str(Decimal(row["gross_booking_value"]) + 1000)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


async def exercise(config: Settings, password: str, viewer_password: str) -> None:
    """Verify controls, filters, lineage, history, access and snapshot navigation in Chrome."""
    async with live_application(config) as origin, async_playwright() as playwright:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        browser = await playwright.chromium.launch(
            headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {})
        )
        try:
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            page = await context.new_page()
            await login(page, origin, "e06-admin", password)
            assert await page.get_by_role(
                "heading", name="Финансовые данные пока не готовы"
            ).is_visible()
            assert (await (await page.request.get(origin + "/api/v1/dashboard")).json())[
                "state"
            ] == "no_snapshot"
            first = await import_cli(config, BASE_DIR / "data")
            await page.reload()
            for start, end, expected, _admitted, _unknown in EXPECTED:
                if start:
                    await page.locator("#portfolio-filters > summary").click()
                    await page.get_by_label("С даты включительно").fill(start.isoformat())
                    await page.get_by_label("По дату включительно").fill(end.isoformat())
                    await page.get_by_role("button", name="Применить", exact=True).click()
                    await page.wait_for_url("**/dashboard?**date_to=" + end.isoformat() + "*")
                await assert_ui_api(page, origin, expected)
            repeat = await import_cli(config, BASE_DIR / "data")
            assert repeat["reused"] and repeat["run_id"] == first["run_id"]
            await page.goto(origin + "/dashboard")
            dashboard_before = await assert_ui_api(page, origin, EXPECTED[0][2])
            quality = await context.new_page()
            await quality.goto(origin + "/quality?limit=1")
            limitations = await context.new_page()
            await limitations.goto(origin + "/quality?limit=1")
            with TemporaryDirectory(prefix="e06_changed_") as temporary:
                changed_copy(Path(temporary))
                second = await import_cli(config, Path(temporary))
            assert second["run_id"] != first["run_id"]
            # These controls were rendered before publication. Their destination must stay pinned.
            await page.locator("#portfolio-filters > summary").click()
            await page.get_by_role("button", name="Применить", exact=True).click()
            await page.wait_for_url("**/dashboard?**")
            after_filter = await assert_ui_api(page, origin, EXPECTED[0][2])
            assert after_filter["run_id"] == first["run_id"], "Dashboard filter switched snapshot"
            assert after_filter["metrics"] == dashboard_before["metrics"]
            await quality.get_by_role("link", name="Следующая страница →", exact=True).click()
            await quality.wait_for_url("**/quality?**offset=1*")
            assert first["run_id"] in await quality.locator(".metadata-details").text_content(), (
                "Quality next page switched snapshot"
            )
            await quality.get_by_role("link", name="← Предыдущая страница", exact=True).click()
            await quality.wait_for_url("**/quality?**offset=0*")
            assert first["run_id"] in await quality.locator(".metadata-details").text_content()
            await limitations.get_by_role("link", name="Посмотреть →", exact=True).click()
            await limitations.wait_for_url("**/quality?**section=limitations*")
            assert (
                first["run_id"] in await limitations.locator(".metadata-details").text_content()
            ), "Limitations switched snapshot"
            await page.locator("#portfolio-filters > summary").click()
            await page.get_by_role("link", name="Сбросить", exact=True).click()
            await page.wait_for_url("**/dashboard?run_id=*")
            assert parse_qs(urlsplit(page.url).query)["run_id"] == [first["run_id"]]
            await page.goto(origin + "/dashboard")
            current = await assert_ui_api(page, origin)
            assert current["run_id"] == second["run_id"]
            assert (
                Decimal(current["metrics"]["gross"]["value"]) == Decimal(EXPECTED[0][2][0]) + 1000
            )
            await page.locator("#portfolio-filters > summary").click()
            await page.locator('select[name="property_id"]').select_option("V019")
            await page.get_by_role("button", name="Применить", exact=True).click()
            await page.wait_for_url("**/dashboard?**property_id=V019*")
            selected = await assert_ui_api(page, origin)
            assert len(selected["properties"]) == 1
            await page.locator(".tabs a").filter(has_text="Операции").click()
            await page.get_by_label("Поиск по ID или объекту").fill("R0002")
            await page.get_by_role("button", name="Найти", exact=True).click()
            await page.get_by_role("link", name="R0002", exact=True).click()
            await page.wait_for_url("**/detail/reservations/R0002?**")
            await page.get_by_text("Правила и формулы", exact=True).click()
            assert "ROUND_HALF_UP" in await page.locator("main").inner_text()
            assert await page.get_by_text(
                "Исходные строки · reservations.csv", exact=True
            ).is_visible()
            await page.get_by_role("link", name="P0001", exact=True).click()
            assert await page.get_by_role("heading", name="P0001", exact=True).is_visible()
            await page.goto(origin + "/quality?business_key=P0051")
            await page.get_by_role("link", name="Поступление не сверено", exact=True).click()
            await page.wait_for_url("**/quality/issues/**")
            assert "R0102" in await page.locator("main").inner_text()
            await page.locator('a[href*="/quality/raw/"]').first.click()
            assert await page.get_by_role(
                "heading", name="Исходная строка", exact=True
            ).is_visible()
            await page.goto(origin + "/imports")
            await page.get_by_role("link", name=second["run_id"][:8], exact=True).click()
            assert await page.get_by_role(
                "heading", name="Сравнение наборов", exact=True
            ).is_visible()
            await page.get_by_text("Изменить базовый снимок", exact=True).click()
            baseline = page.get_by_label("Базовый опубликованный run_id", exact=True)
            assert await baseline.input_value() == first["run_id"]
            await baseline.fill(first["run_id"])
            await page.get_by_role("button", name="Сравнить", exact=True).click()
            await page.wait_for_url("**/imports/**?base_run_id=*")
            comparison = await (
                await page.request.get(
                    origin
                    + "/api/v1/imports/"
                    + second["run_id"]
                    + "?base_run_id="
                    + first["run_id"]
                )
            ).json()
            assert comparison["comparison"]["reservations.csv"]["changed"] == ["R0002"]
            await (
                page.get_by_role("row")
                .filter(has_text="reservations.csv")
                .locator("td")
                .nth(2)
                .locator("summary")
                .click()
            )
            assert "R0002" in await page.locator("main").inner_text()
            await page.locator(".user-menu > summary").click()
            await page.get_by_role("button", name="Выйти", exact=True).click()
            await page.wait_for_url("**/login")
            assert (await page.request.get(origin + "/api/v1/dashboard")).status == 401
            await login(page, origin, "e06-viewer", viewer_password)
            scoped = await assert_ui_api(page, origin)
            assert scoped["scope"] == ["V019"]
            for route, status in (
                ("/api/v1/dashboard?property_id=V001", 403),
                ("/api/v1/reservations/R0001", 404),
                ("/api/v1/quality-issues?property_id=V001", 403),
                ("/api/v1/imports", 403),
                ("/imports", 403),
            ):
                assert (await page.request.get(origin + route)).status == status
            await page.goto(origin + "/dashboard?date_from=2027-01-01&date_to=2027-01-31")
            assert (await assert_ui_api(page, origin))["state"] == "empty"
            await page.locator(".user-menu > summary").click()
            await page.get_by_role("button", name="Выйти", exact=True).click()
            await page.wait_for_url("**/login")
            assert (await page.request.get(origin + "/api/v1/quality-issues")).status == 401
            print(
                "E06 Chrome: isolated real CLI/PG/CH, all/monthly UI/API controls, repeat/change, "
                "pinned filters/pagination, property/detail/raw/history, scope, empty and logout passed."
            )
        finally:
            await browser.close()


async def main() -> None:
    """Migrate unique databases, create temporary users, run Chrome and drop all test data."""
    async with disposable_databases() as config, migrated_environment(config) as (service, _):
        password = await seed(service.database, config, "e06-admin", "admin", [])
        viewer_password = await seed(service.database, config, "e06-viewer", "viewer", ["V019"])
        await exercise(config, password, viewer_password)


if __name__ == "__main__":
    asyncio.run(main())
