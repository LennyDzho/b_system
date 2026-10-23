"""Exercise the actual E04 browser flow with a temporary scoped CLI-created account."""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from src.core.settings import BASE_DIR, settings
from tests.browser_smoke import remove_user


def main() -> None:
    """Verify rendered filters, warnings, amounts, provenance, scope and logout without printing secrets."""
    username = "finance-browser-" + secrets.token_hex(6)
    environment = os.environ.copy()
    password = secrets.token_urlsafe(24)
    environment["E04_BROWSER_PASSWORD"] = password
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli.create_user",
            "--username",
            username,
            "--role",
            "viewer",
            "--property-id",
            "V019",
            "--password-env",
            "E04_BROWSER_PASSWORD",
        ],
        env=environment,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Temporary finance browser account could not be created")
    try:
        with sync_playwright() as playwright:
            chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
            browser = playwright.chromium.launch(
                headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {})
            )
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(settings.auth.origin + "/dashboard")
                page.wait_for_url("**/login")
                page.get_by_label("Логин").fill(username)
                page.get_by_label("Пароль").fill(password)
                page.get_by_role("button", name="Войти").click()
                page.wait_for_url("**/dashboard")
                assert page.get_by_role("heading", name="Финансы портфеля", exact=True).is_visible()
                page.locator("#trend-chart svg").wait_for()
                assert page.locator(".chart-plot svg").count() == 3
                scope_data = page.request.get(settings.auth.origin + "/api/v1/dashboard").json()
                assert scope_data["scope"] == ["V019"]
                assert (
                    page.get_by_label("С даты включительно").input_value()
                    == scope_data["date_from"]
                )
                assert (
                    "Результат собственника"
                    in page.locator("[data-metric=owner_estimate]").inner_text()
                )
                for warning in scope_data["warnings"]:
                    assert warning not in page.locator("main").inner_text()
                page.locator("#portfolio-filters > summary").click()
                page.get_by_label("С даты включительно").fill("2026-09-01")
                page.get_by_label("По дату включительно").fill("2026-09-30")
                page.get_by_role("button", name="Применить").click()
                page.wait_for_url("**/dashboard?**date_to=2026-09-30*")
                assert page.get_by_label("По дату включительно").input_value() == "2026-09-30"
                assert (
                    page.request.get(
                        settings.auth.origin + "/api/v1/dashboard?property_id=V001"
                    ).status
                    == 403
                )
                assert (
                    page.request.get(settings.auth.origin + "/api/v1/reservations/R0001").status
                    == 404
                )
                page.locator("#portfolio-filters > summary").click()
                page.get_by_role("link", name="Сбросить").click()
                page.wait_for_url("**/dashboard?run_id=*")
                output = BASE_DIR / ".cache" / "e04-dashboard.png"
                output.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(output))
                page.locator(".tabs a").filter(has_text="Операции").click()
                page.get_by_label("Поиск по ID или объекту").fill("R0002")
                page.get_by_role("button", name="Найти", exact=True).click()
                page.get_by_role("link", name="R0002", exact=True).click()
                page.wait_for_url("**/detail/reservations/R0002?**")
                assert page.get_by_role("heading", name="R0002", exact=True).is_visible()
                assert "2 002 396" in page.locator(".values").inner_text()
                assert page.get_by_text(
                    "Исходные строки · reservations.csv", exact=True
                ).is_visible()
                page.get_by_text("Правила и формулы", exact=True).click()
                assert "ROUND_HALF_UP" in page.locator("main").inner_text()
                page.get_by_role("link", name="P0001", exact=True).click()
                assert page.get_by_role("heading", name="P0001", exact=True).is_visible()
                assert "10 011 978" in page.locator(".values").inner_text()
                page.goto(
                    settings.auth.origin + "/dashboard?date_from=2027-01-01&date_to=2027-01-31"
                )
                assert page.get_by_role("heading", name="В этой выборке нет записей").is_visible()
                assert page.locator("[data-metric=cash] strong").inner_text() == "0"
                page.locator(".user-menu > summary").click()
                page.get_by_role("button", name="Выйти").click()
                page.wait_for_url("**/login")
                assert page.request.get(settings.auth.origin + "/api/v1/dashboard").status == 401
                print(
                    "E04 browser: scoped dashboard, dates, warnings, raw/formulas, linked cash, empty period and logout passed."
                )
            finally:
                browser.close()
    finally:
        asyncio.run(remove_user(username))


if __name__ == "__main__":
    main()
