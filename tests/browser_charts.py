"""Exercise chart controls and financial scope in Chrome against disposable real databases."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from playwright.async_api import async_playwright

from src.core.settings import BASE_DIR
from tests.browser_end_to_end import import_cli, live_application
from tests.integration.test_auth import seed
from tests.real_environment import disposable_databases, migrated_environment


async def main() -> None:
    """Verify real import, exact series, accessible controls, responsive plots and no-data states."""
    async with disposable_databases() as config:
        async with migrated_environment(config) as (importer, config):
            published = await import_cli(config, BASE_DIR / "data")
            password = await seed(importer.database, config, "chart-viewer", "viewer", ["V019"])
            empty_password = await seed(importer.database, config, "chart-empty", "viewer", [])
            admin_password = await seed(importer.database, config, "chart-admin", "admin", [])
            async with live_application(config) as origin:
                async with async_playwright() as playwright:
                    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
                    browser = await playwright.chromium.launch(
                        headless=True,
                        **({"executable_path": str(chrome)} if chrome.is_file() else {}),
                    )
                    try:
                        page = await browser.new_page(viewport={"width": 1440, "height": 1050})
                        errors: list[str] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(origin + "/login")
                        await page.get_by_label("Логин", exact=True).fill("chart-viewer")
                        await page.get_by_label("Пароль", exact=True).fill(password)
                        await page.get_by_role("button", name="Войти", exact=True).click()
                        await page.wait_for_url("**/dashboard")
                        await page.locator("#properties-chart svg").wait_for()
                        assert await page.locator(".chart-plot svg").count() == 4
                        payload = json.loads(
                            await page.locator("#dashboard-chart-data").text_content()
                        )
                        api = await (await page.request.get(origin + "/api/v1/dashboard")).json()
                        assert payload["run_id"] == api["run_id"] == published["run_id"]
                        assert payload["charts"] == api["charts"]
                        assert [p["property_id"] for p in payload["properties"]] == ["V019"]
                        assert (
                            await page.request.get(origin + "/api/v1/dashboard?property_id=V001")
                        ).status == 403
                        for series in api["charts"].values():
                            for key in api["metrics"]:
                                assert sum(
                                    Decimal(p["metrics"][key]["known_subtotal"])
                                    for p in series["points"]
                                ) == Decimal(api["metrics"][key]["known_subtotal"])

                        await page.get_by_role(
                            "button", name="Свернуть: Динамика основных показателей", exact=True
                        ).click()
                        assert not await page.locator("#trend-chart").is_visible()
                        await page.reload()
                        assert not await page.locator("#trend-chart").is_visible()
                        toggle = page.get_by_role(
                            "button", name="Развернуть: Динамика основных показателей", exact=True
                        )
                        await toggle.focus()
                        await page.keyboard.press("Enter")
                        assert await page.locator("#trend-chart").is_visible()
                        before = await page.locator("#trend-chart svg").inner_html()
                        legend_button = page.locator("#trend-panel .legend-toggle")
                        await legend_button.click()
                        assert not await page.locator("#trend-legend").is_visible()
                        assert await page.locator("#trend-chart svg").inner_html() == before
                        await legend_button.focus()
                        await page.keyboard.press("Space")
                        assert await page.locator("#trend-legend").is_visible()
                        gross_button = page.locator('#trend-legend [data-series="gross"]')
                        await gross_button.click()
                        assert await gross_button.get_attribute("aria-pressed") == "false"
                        assert (
                            await page.locator('#trend-chart path[data-series="gross"]').count()
                            == 0
                        )
                        await gross_button.click()
                        assert (
                            await page.locator('#trend-chart path[data-series="gross"]').count()
                            == 1
                        )
                        management = page.locator('#trend-legend [data-series="management"]')
                        await management.click()
                        await page.locator("#trend-panel .chart-table summary").click()
                        assert await page.locator("#trend-panel .chart-table table").is_visible()
                        await page.locator("#trend-panel .chart-table summary").click()

                        await page.get_by_role(
                            "button", name="Свернуть: Финансовая детализация", exact=True
                        ).click()
                        assert not await page.locator("#records table").is_visible()
                        await page.locator('[data-metric="gross"]').click()
                        assert await page.locator("#records table").is_visible()
                        await page.get_by_role(
                            "button", name="Свернуть все блоки", exact=True
                        ).click()
                        assert not await page.locator("#trend-chart").is_visible()
                        await page.get_by_role(
                            "button", name="Развернуть все блоки", exact=True
                        ).click()
                        assert await page.locator("#trend-chart").is_visible()
                        screenshot = BASE_DIR / ".cache" / "dashboard-charts-desktop.png"
                        await page.locator("#trend-panel").screenshot(path=str(screenshot))
                        await page.set_viewport_size({"width": 390, "height": 844})
                        await page.locator("#trend-panel").screenshot(
                            path=str(BASE_DIR / ".cache" / "dashboard-charts-mobile.png")
                        )
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )

                        negative = next(
                            point
                            for point in api["charts"]["trend"]["points"]
                            if Decimal(point["metrics"]["owner_estimate"]["value"]) < 0
                        )
                        await page.goto(
                            origin
                            + "/dashboard?date_from="
                            + negative["date_from"]
                            + "&date_to="
                            + negative["date_to"]
                        )
                        await page.locator("#trend-chart svg").wait_for()
                        assert await page.locator("#trend-period option").count() == 1
                        assert "-" in await page.locator("#trend-readout").inner_text()
                        assert not await page.locator(".chart-plot svg").evaluate_all(
                            "items => items.some(item => /NaN|Infinity/.test(item.outerHTML))"
                        )
                        await page.goto(
                            origin + "/dashboard?date_from=1900-01-01&date_to=2299-12-31"
                        )
                        await page.locator("#trend-chart svg").wait_for()
                        assert await page.locator("#trend-period option").count() <= 128
                        await page.goto(
                            origin + "/dashboard?date_from=2099-01-01&date_to=2099-01-31"
                        )
                        assert await page.locator("#charts-empty").is_visible()
                        assert await page.locator(".chart-plot svg").count() == 0
                        # JSON login shares the browser cookie jar, including an explicitly empty scope.
                        response = await page.request.post(
                            origin + "/api/v1/auth/login",
                            headers={"Origin": origin},
                            data={"username": "chart-empty", "password": empty_password},
                        )
                        assert response.status == 200
                        await page.goto(origin + "/dashboard")
                        assert await page.locator("#charts-empty").is_visible()
                        assert await page.locator("#dashboard-chart-data").count() == 0
                        response = await page.request.post(
                            origin + "/api/v1/auth/login",
                            headers={"Origin": origin},
                            data={"username": "chart-admin", "password": admin_password},
                        )
                        assert response.status == 200
                        await page.goto(origin + "/dashboard")
                        await page.locator("#trend-chart svg").wait_for()
                        portfolio = await (
                            await page.request.get(origin + "/api/v1/dashboard")
                        ).json()
                        await page.locator('#trend-legend [data-series="management"]').click()
                        unknown_index = next(
                            index
                            for index, point in enumerate(portfolio["charts"]["trend"]["points"])
                            if point["metrics"]["management"]["value"] is None
                        )
                        await page.get_by_label("Период на графике", exact=True).select_option(
                            str(unknown_index)
                        )
                        assert (
                            "Не определено; известная часть"
                            in await page.locator("#trend-readout").inner_text()
                        )
                        assert not errors, errors

                        no_js = await browser.new_context(java_script_enabled=False)
                        try:
                            response = await no_js.request.post(
                                origin + "/api/v1/auth/login",
                                headers={"Origin": origin},
                                data={"username": "chart-viewer", "password": password},
                            )
                            assert response.status == 200
                            no_js_page = await no_js.new_page()
                            await no_js_page.goto(origin + "/dashboard")
                            assert await no_js_page.locator(
                                "#trend-panel .chart-table summary"
                            ).is_visible()
                            await no_js_page.locator("#trend-panel .chart-table summary").click()
                            assert await no_js_page.locator(
                                "#trend-panel .chart-table table"
                            ).is_visible()
                        finally:
                            await no_js.close()
                        print(
                            "Charts Chrome: real import/PG/CH, API parity/scope, collapse/reload/keyboard/anchors, legends/series/unknown fees, exact tables, negative/single-day/long/empty periods and mobile/no-JS passed."
                        )
                    finally:
                        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
