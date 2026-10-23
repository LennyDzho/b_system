"""Verify the redesigned HTML workflows in Chrome against disposable PostgreSQL/ClickHouse."""

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Page, async_playwright

from src.core.settings import BASE_DIR
from tests.browser_end_to_end import import_cli, live_application, login
from tests.integration.test_auth import seed
from tests.real_environment import disposable_databases, migrated_environment
from tests.test_finance import EXPECTED, METRICS
from web.templating import money


async def logout(page: Page) -> None:
    """Revoke the session through the visible account menu."""
    await page.locator(".user-menu > summary").click()
    await page.get_by_role("button", name="Выйти", exact=True).click()
    await page.wait_for_url("**/login")


async def parity(page: Page, origin: str, expected: list[int] | None = None) -> dict:
    """Compare exact HTML/card/chart values with the same pinned application response."""
    response = await page.request.get(origin + "/api/v1/dashboard?" + urlsplit(page.url).query)
    assert response.status == 200
    data = await response.json()
    payload = json.loads(await page.locator("#dashboard-chart-data").text_content())
    assert payload["run_id"] == data["run_id"] and payload["charts"] == data["charts"]
    for key in METRICS:
        metric = data["metrics"][key]
        value = metric["known_subtotal"] if key == "owner_estimate" else metric["value"]
        assert await page.locator(f"[data-metric={key}] strong").text_content() == money(value)
    if expected:
        assert [Decimal(data["metrics"][key]["known_subtotal"]) for key in METRICS] == expected
    if expected:
        control = next(row for row in EXPECTED if row[2] == expected)
        assert data["coverage"]["reservations.csv"]["included"] == control[3]
        assert data["unknown_fees"] == control[4]
    return data


async def main() -> None:
    """Check financial parity, drill-down, navigation, responsiveness, no-JS forms and access."""
    async with disposable_databases() as config:
        async with migrated_environment(config) as (importer, config):
            passwords = {
                role: await seed(importer.database, config, "design-" + role, access, ids)
                for role, access, ids in (
                    ("admin", "admin", []),
                    ("viewer", "viewer", ["V019"]),
                    ("empty", "viewer", []),
                )
            }
            published = await import_cli(config, BASE_DIR / "data")
            async with live_application(config) as origin, async_playwright() as playwright:
                chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
                browser = await playwright.chromium.launch(
                    headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {})
                )
                try:
                    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                    page = await context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await login(page, origin, "design-admin", passwords["admin"])
                    data = await parity(page, origin, EXPECTED[0][2])
                    assert data["run_id"] == published["run_id"]
                    assert await page.locator("#records").count() == 0
                    assert await page.locator("#trend-chart svg").is_visible()
                    assert not await page.locator("#cash-chart").is_visible()
                    await page.locator("#cash-chart-panel .panel-toggle").click()
                    assert await page.locator("#cash-chart svg").is_visible()
                    legend = page.locator("#trend-panel .legend-toggle")
                    await legend.click()
                    assert not await page.locator("#trend-legend").is_visible()
                    await legend.click()
                    await page.locator('#trend-legend [data-series="gross"]').click()
                    assert await page.locator('#trend-chart path[data-series="gross"]').count() == 0
                    await page.locator('#trend-legend [data-series="gross"]').click()
                    for width in (320, 390, 768, 1024, 1440):
                        await page.set_viewport_size({"width": width, "height": 900})
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                        assert await page.locator(".metrics .card").first.evaluate(
                            "e => e.getBoundingClientRect().top + scrollY < 600"
                        )
                    output = BASE_DIR / ".cache" / "design"
                    output.mkdir(parents=True, exist_ok=True)
                    await page.screenshot(path=str(output / "overview-desktop.png"))
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.screenshot(path=str(output / "overview-mobile.png"))
                    await page.set_viewport_size({"width": 1440, "height": 1000})
                    for start, end, totals, _, _ in EXPECTED[1:]:
                        await page.locator("#portfolio-filters > summary").click()
                        await page.get_by_label("С даты включительно", exact=True).fill(
                            start.isoformat()
                        )
                        await page.get_by_label("По дату включительно", exact=True).fill(
                            end.isoformat()
                        )
                        async with page.expect_navigation():
                            await page.get_by_role("button", name="Применить", exact=True).click()
                        await parity(page, origin, totals)
                    await page.goto(origin + "/dashboard")
                    await page.locator("#metrics-panel .panel-toggle").click()
                    await page.locator('[data-metric="refund"]').click()
                    assert "view=operations" in page.url and "metric=refund" in page.url
                    assert await page.locator("#records tbody tr").count() < 315
                    assert set(
                        await page.locator("#records tbody tr").evaluate_all(
                            "rows => rows.map(r => r.dataset.source)"
                        )
                    ) == {"reservations.csv"}
                    await page.goto(origin + "/dashboard?view=operations")
                    first = set(
                        await page.locator("#records tbody tr").evaluate_all(
                            "rows => rows.map(r => r.dataset.key)"
                        )
                    )
                    assert len(first) == 25
                    await page.get_by_role("link", name="Далее →", exact=True).click()
                    second = set(
                        await page.locator("#records tbody tr").evaluate_all(
                            "rows => rows.map(r => r.dataset.key)"
                        )
                    )
                    assert len(second) == 25 and first.isdisjoint(second)
                    await page.goto(
                        origin + "/dashboard?view=operations&property_id=V019"
                        "&date_from=2026-10-01&date_to=2026-10-31&source=expenses.csv"
                    )
                    original = parse_qs(urlsplit(page.url).query)
                    assert (
                        await page.locator(
                            '#portfolio-filters select[name="property_id"] option'
                        ).count()
                        == 21
                    )
                    await page.locator("#records tbody a").first.click()
                    assert (
                        await page.locator(".values")
                        .get_by_text("Не определено", exact=True)
                        .count()
                        == 0
                    )
                    await page.get_by_role("link", name="← Назад к финансам", exact=True).click()
                    restored = parse_qs(urlsplit(page.url).query)
                    assert all(restored[key] == value for key, value in original.items())
                    assert restored["run_id"] == [published["run_id"]]
                    await page.goto(origin + "/dashboard?view=properties")
                    assert await page.locator("#properties-chart svg").is_visible()
                    assert (
                        await page.locator("#properties-chart svg text")
                        .filter(has_text="V020")
                        .count()
                        == 0
                    )
                    await page.locator("#property-chart-count").select_option("all")
                    assert (
                        await page.locator("#properties-chart svg text")
                        .filter(has_text="V020")
                        .count()
                        == 1
                    )
                    await page.goto(origin + "/quality")
                    await page.locator("#quality-filters > summary").click()
                    await page.get_by_text("Дополнительные фильтры", exact=True).click()
                    await page.get_by_label("Код проблемы", exact=True).select_option(
                        "OCCUPANCY_OVERLAP"
                    )
                    await page.get_by_role("button", name="Применить", exact=True).click()
                    api = await (
                        await page.request.get(
                            origin + "/api/v1/quality-issues?" + urlsplit(page.url).query
                        )
                    ).json()
                    assert await page.locator("[data-count=issues] strong").text_content() == str(
                        api["total"]
                    )
                    await page.locator("tbody a").first.click()
                    assert await page.get_by_role(
                        "heading", name="Пересечение проживаний", exact=True
                    ).is_visible()
                    await page.locator('a[href*="/quality/raw/"]').first.click()
                    assert await page.get_by_role(
                        "heading", name="Исходная строка", exact=True
                    ).is_visible()
                    await page.goto(origin + "/imports")
                    await page.locator("#import-history tbody a").first.click()
                    assert await page.get_by_role(
                        "heading", name="Сравнение наборов", exact=True
                    ).is_visible()
                    assert await page.locator("pre:visible").count() == 0
                    await page.goto(origin + "/dashboard?date_from=2099-01-01&date_to=2099-01-31")
                    assert await page.locator("#charts-empty").is_visible()
                    assert await page.locator('[data-metric="cash"] strong').text_content() == "0"
                    await logout(page)

                    for role in ("viewer", "empty"):
                        await login(page, origin, "design-" + role, passwords[role])
                        await page.goto(origin + "/dashboard?view=operations")
                        assert await page.locator("#unallocated").count() == 0
                        assert (await page.request.get(origin + "/imports")).status == 403
                        assert (
                            await page.request.get(origin + "/dashboard?property_id=V001")
                        ).status == 403
                        assert (
                            await page.request.get(origin + "/detail/payments/P0017")
                        ).status == 404
                        if role == "viewer":
                            assert set(
                                await page.locator("#records tbody tr").evaluate_all(
                                    "rows => rows.map(r => r.cells[1].textContent.trim())"
                                )
                            ) == {"V019"}
                        else:
                            assert await page.locator("#records tbody tr").count() == 0
                        await logout(page)
                    assert (await page.request.get(origin + "/api/v1/dashboard")).status == 401

                    no_js = await browser.new_context(java_script_enabled=False)
                    await no_js.request.post(
                        origin + "/api/v1/auth/login",
                        headers={"Origin": origin},
                        data={"username": "design-admin", "password": passwords["admin"]},
                    )
                    native = await no_js.new_page()
                    for destination, summary in (
                        ("/dashboard?view=operations", None),
                        ("/quality", "#quality-filters > summary"),
                        ("/imports", None),
                    ):
                        await native.goto(origin + destination)
                        if summary:
                            await native.locator(summary).click()
                        form = native.locator(
                            "#operations-filter"
                            if "operations" in destination
                            else "#finance-filter"
                        )
                        async with native.expect_navigation() as response:
                            await form.locator("button[type=submit]").click()
                        assert (await response.value).status == 200
                    await native.goto(origin + "/dashboard")
                    await native.locator("#trend-panel .chart-table > summary").click()
                    assert await native.locator("#trend-panel .chart-table table").is_visible()
                    await no_js.close()
                    assert not errors, errors
                    print(
                        "Design Chrome: real import/PG/CH, 40 financial controls, UI/API snapshot parity, "
                        "metric selection, pagination, filters/back context, charts/legends, source links, "
                        "history, 320–1440px, no-JS native forms, viewer/empty scopes and logout passed."
                    )
                finally:
                    await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
