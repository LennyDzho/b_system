"""Exercise administrator CSV uploads through the real browser and isolated PostgreSQL/ClickHouse."""

import asyncio
from decimal import Decimal
from pathlib import Path

from playwright.async_api import Route, async_playwright

from src.core.settings import BASE_DIR
from src.services.imports.records import SCHEMAS
from tests.browser_end_to_end import live_application
from tests.integration.test_auth import seed
from tests.real_environment import disposable_databases, migrated_environment


async def main() -> None:
    """Verify first upload, real publication, reuse, invalid CSV, audit links and viewer denial."""
    async with disposable_databases() as config:
        async with migrated_environment(config) as (importer, config):
            password = await seed(importer.database, config, "upload-admin", "admin", [])
            viewer_password = await seed(importer.database, config, "upload-viewer", "viewer", [])
            async with live_application(config) as origin:
                async with async_playwright() as playwright:
                    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
                    browser = await playwright.chromium.launch(
                        headless=True,
                        **({"executable_path": str(chrome)} if chrome.is_file() else {}),
                    )
                    try:
                        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(origin + "/login")
                        await page.get_by_label("Логин", exact=True).fill("upload-admin")
                        await page.get_by_label("Пароль", exact=True).fill(password)
                        await page.get_by_role("button", name="Войти", exact=True).click()
                        await page.wait_for_url("**/dashboard")
                        assert "пока не готовы" in await page.locator("main").inner_text()
                        await page.goto(origin + "/imports?view=upload")
                        await page.get_by_label(
                            "CSV-файлы полного набора", exact=True
                        ).set_input_files([str(BASE_DIR / "data" / name) for name in SCHEMAS])
                        started, release = asyncio.Event(), asyncio.Event()

                        async def hold_upload(route: Route) -> None:
                            """Pause transport to inspect busy UI, then continue the real unmocked request."""
                            started.set()
                            await release.wait()
                            await route.continue_()

                        await page.route("**/api/v1/imports", hold_upload)
                        async with page.expect_response(
                            lambda response: response.request.method == "POST"
                            and response.url == origin + "/api/v1/imports",
                            timeout=60000,
                        ) as received:
                            await page.get_by_role(
                                "button", name="Импортировать набор", exact=True
                            ).click()
                            try:
                                await asyncio.wait_for(started.wait(), 10)
                                assert await page.locator("#import-submit").is_disabled()
                                assert await page.locator("#import-files").is_disabled()
                                assert (
                                    await page.locator("#import-upload-form").get_attribute(
                                        "aria-busy"
                                    )
                                    == "true"
                                )
                            finally:
                                release.set()
                        response = await received.value
                        assert response.status == 200
                        first = await response.json()
                        assert not first["reused"] and first["summary"]["counts"]["raw"] == 357
                        await page.locator('#import-result[data-outcome="published"]').wait_for()
                        await page.unroute("**/api/v1/imports", hold_upload)
                        assert not await page.locator("#import-submit").is_disabled()
                        assert first["run_id"] in await page.locator("#import-result").inner_text()
                        assert (
                            first["run_id"][:8]
                            in await page.locator("#import-history").text_content()
                        )
                        assert await page.locator("#import-count").text_content() == "1"
                        await page.locator("#import-upload").screenshot(
                            path=str(BASE_DIR / ".cache" / "import-ui.png")
                        )
                        await page.get_by_role("link", name="Результат набора", exact=True).click()
                        assert await page.get_by_role(
                            "heading", name="Сравнение наборов", exact=True
                        ).is_visible()
                        assert "web:" in await page.locator("main").inner_text()
                        await page.goto(origin + "/imports?view=upload")
                        await page.get_by_label(
                            "CSV-файлы полного набора", exact=True
                        ).set_input_files([str(BASE_DIR / "data" / name) for name in SCHEMAS])
                        await page.get_by_role(
                            "button", name="Импортировать набор", exact=True
                        ).click()
                        await page.locator('#import-result[data-outcome="reused"]').wait_for()
                        assert (await (await page.request.get(origin + "/api/v1/imports")).json())[
                            "total"
                        ] == 1
                        finance_destination = await page.get_by_role(
                            "link", name="Финансы активного снимка", exact=True
                        ).get_attribute("href")
                        assert finance_destination
                        await page.goto(origin + "/imports")
                        for selected in ("", "published", ""):
                            await page.locator(
                                '#finance-filter select[name="status"]'
                            ).select_option(selected)
                            async with page.expect_response(
                                lambda response: response.request.is_navigation_request()
                                and response.url.startswith(origin + "/imports")
                            ) as navigation:
                                await page.get_by_role(
                                    "button", name="Применить", exact=True
                                ).click()
                            assert (await navigation.value).status == 200
                            await page.wait_for_load_state("domcontentloaded")
                            if not selected:
                                assert "status=" not in page.url
                            assert await page.get_by_role(
                                "heading", name="История импортов", exact=True
                            ).is_visible()
                        await page.goto(origin + finance_destination)
                        await page.locator("#trend-chart svg").wait_for()
                        dashboard = await (
                            await page.request.get(origin + "/api/v1/dashboard")
                        ).json()
                        assert dashboard["run_id"] == first["run_id"]
                        assert (
                            Decimal(dashboard["metrics"]["owner_estimate"]["known_subtotal"])
                            == 785128468
                        )
                        await page.get_by_role("link", name="Качество данных", exact=True).click()
                        assert await page.get_by_role(
                            "heading", name="Качество данных", exact=True
                        ).is_visible()
                        await page.goto(origin + "/imports?view=upload")
                        await page.get_by_label(
                            "CSV-файлы полного набора", exact=True
                        ).set_input_files(
                            [str(BASE_DIR / "data" / name) for name in list(SCHEMAS)[:-1]]
                        )
                        await page.get_by_role(
                            "button", name="Импортировать набор", exact=True
                        ).click()
                        await page.locator('#import-result[data-outcome="invalid"]').wait_for()
                        bad_files = [
                            {
                                "name": name,
                                "mimeType": "text/csv",
                                "buffer": b"bad,header\n"
                                if name == "expenses.csv"
                                else (BASE_DIR / "data" / name).read_bytes(),
                            }
                            for name in SCHEMAS
                        ]
                        await page.get_by_label(
                            "CSV-файлы полного набора", exact=True
                        ).set_input_files(bad_files)
                        await page.get_by_role(
                            "button", name="Импортировать набор", exact=True
                        ).click()
                        await page.locator('#import-result[data-outcome="failed"]').wait_for()
                        assert await page.get_by_role(
                            "link", name="Открыть попытку импорта", exact=True
                        ).is_visible()
                        history = await (await page.request.get(origin + "/api/v1/imports")).json()
                        assert history["total"] == 2 and history["active_run_id"] == first["run_id"]
                        assert await page.locator("#import-count").text_content() == "2"
                        await page.set_viewport_size({"width": 390, "height": 844})
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )
                        response = await page.request.post(
                            origin + "/api/v1/auth/login",
                            headers={"Origin": origin},
                            data={"username": "upload-viewer", "password": viewer_password},
                        )
                        assert response.status == 200
                        await page.goto(origin + "/dashboard")
                        assert (
                            await page.get_by_role("link", name="Импорт CSV", exact=True).count()
                            == 0
                        )
                        assert (
                            await page.request.post(
                                origin + "/api/v1/imports",
                                headers={"Origin": origin},
                                data="invalid",
                            )
                        ).status == 403
                        assert (await page.request.get(origin + "/imports")).status == 403
                        assert not errors, errors
                        print(
                            "Import Chrome: empty start, real five-CSV upload/PG/CH/publication, busy controls, actor/history/dashboard/quality, repeat without duplication, missing/bad CSV, mobile and viewer denial passed."
                        )
                    finally:
                        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
