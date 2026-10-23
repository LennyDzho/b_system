"""Verify real E05 navigation, issue relationships and admin-only history in Chrome."""

import asyncio
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from src.core.settings import BASE_DIR, settings
from tests.browser_smoke import remove_user


def create_account(role: str) -> tuple[str, str]:
    """Create a random CLI account without passing its password as a command argument."""
    username, password = "quality-browser-" + secrets.token_hex(6), secrets.token_urlsafe(24)
    environment = os.environ.copy()
    environment["E05_BROWSER_PASSWORD"] = password
    args = [
        sys.executable,
        "-m",
        "src.cli.create_user",
        "--username",
        username,
        "--role",
        role,
        "--password-env",
        "E05_BROWSER_PASSWORD",
    ]
    if role == "viewer":
        args.extend(["--property-id", "V008"])
    result = subprocess.run(
        args, env=environment, cwd=BASE_DIR, capture_output=True, text=True, timeout=30
    )
    if result.returncode:
        raise RuntimeError("Temporary quality account could not be created")
    return username, password


def check_unallocated(page: Page) -> None:
    """Follow real admin source/detail links and keep period/object filters bound to one run."""
    dashboard = page.request.get(settings.auth.origin + "/api/v1/dashboard").json()
    run_id = dashboard["run_id"]
    section = page.locator("#unallocated")
    assert section.locator("tbody tr").count() == 3
    output = BASE_DIR / ".cache" / "unallocated.png"
    output.parent.mkdir(exist_ok=True)
    section.screenshot(path=str(output))
    for source, key, amount, property_id in (
        ("expenses.csv", "E0017", "834 779", "V777"),
        ("payments.csv", "P0017", "12 727 390", "V999"),
        ("reservations.csv", "R0034", "14 462 943", "V999"),
    ):
        row = section.get_by_role("row").filter(has=page.get_by_role("link", name=key, exact=True))
        assert amount in row.inner_text()
        assert "Объект отсутствует в достоверном справочнике" in row.inner_text()
        raw = row.get_by_role("link", name=re.compile("^Строка ")).first
        assert f"run_id={run_id}" in raw.get_attribute("href")
        raw.click()
        assert page.get_by_role("heading", name="Исходная строка", exact=True).is_visible()
        assert property_id in page.locator("main").inner_text()
        page.go_back()
        row.get_by_role("link", name=key, exact=True).click()
        assert page.get_by_role("heading", name=key, exact=True).is_visible()
        assert source in page.locator("main").inner_text()
        assert "Исключено из распределённых сумм" in page.locator("main").inner_text()
        page.go_back()
    page.set_viewport_size({"width": 390, "height": 844})
    section.scroll_into_view_if_needed()
    assert section.is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.set_viewport_size({"width": 1500, "height": 1000})
    page.locator("#portfolio-filters > summary").click()
    page.get_by_label("С даты включительно").fill("2026-10-20")
    page.get_by_label("По дату включительно").fill("2026-10-21")
    page.get_by_role("button", name="Применить", exact=True).click()
    page.wait_for_url("**/dashboard?**date_to=2026-10-21*")
    assert section.locator("tbody tr").count() == 2
    assert section.get_by_role("link", name="R0034", exact=True).count() == 0
    assert section.get_by_role("link", name="P0017", exact=True).is_visible()
    assert section.get_by_role("link", name="E0017", exact=True).is_visible()
    page.locator("#portfolio-filters > summary").click()
    page.locator("#finance-filter select[name=property_id]").select_option("V019")
    page.get_by_role("button", name="Применить", exact=True).click()
    page.wait_for_url("**/dashboard?**property_id=V019*")
    assert section.count() == 0
    page.goto(settings.auth.origin + f"/dashboard?run_id={run_id}")


def main() -> None:
    """Exercise scoped issues, overlap links, cash/accrual decisions, raw, history and comparison."""
    accounts: list[tuple[str, str]] = []
    try:
        accounts.extend([create_account("viewer")])
        accounts.extend([create_account("admin")])
        with sync_playwright() as playwright:
            chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
            browser = playwright.chromium.launch(
                headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {})
            )
            try:
                page = browser.new_page(viewport={"width": 1500, "height": 1000})
                for index, (username, password) in enumerate(accounts):
                    page.goto(settings.auth.origin + "/login")
                    page.get_by_label("Логин").fill(username)
                    page.get_by_label("Пароль").fill(password)
                    page.get_by_role("button", name="Войти").click()
                    page.wait_for_url("**/dashboard")
                    if index == 0:
                        assert page.locator("#unallocated").count() == 0
                        assert (
                            page.request.get(
                                settings.auth.origin + "/quality/raw/expenses.csv/18"
                            ).status
                            == 404
                        )
                    else:
                        page.locator(".tabs a").filter(has_text="Операции").click()
                        check_unallocated(page)
                        page.locator("#portfolio-filters > summary").click()
                        page.get_by_role("link", name="Сбросить", exact=True).click()
                    page.get_by_role("link", name="Качество данных", exact=True).click()
                    assert page.get_by_role(
                        "heading", name="Качество данных", exact=True
                    ).is_visible()
                    page.locator("#quality-filters > summary").click()
                    page.get_by_text("Дополнительные фильтры", exact=True).click()
                    if index == 0:
                        assert (
                            page.request.get(settings.auth.origin + "/api/v1/imports").status == 403
                        )
                        assert (
                            page.get_by_role("link", name="История импортов", exact=True).count()
                            == 0
                        )
                        page.get_by_label("Код проблемы", exact=True).select_option(
                            "OCCUPANCY_OVERLAP"
                        )
                        page.get_by_role("button", name="Применить", exact=True).click()
                        page.wait_for_url("**/quality?**code=OCCUPANCY_OVERLAP*")
                        assert (
                            page.locator("[data-count=affected_entities] strong").inner_text()
                            != "87"
                        )
                        pair = page.get_by_role("row").filter(has_text="R0033 и R0056")
                        pair.get_by_role("link", name="Пересечение проживаний", exact=True).click()
                        assert page.get_by_role(
                            "heading", name="Пересечение проживаний", exact=True
                        ).is_visible()
                        assert "R0056" in page.locator("main").inner_text()
                        page.get_by_role("link", name=re.compile("^Исходная строка ")).first.click()
                        assert page.get_by_role(
                            "heading", name="Исходная строка", exact=True
                        ).is_visible()
                    else:
                        page.get_by_label("Бизнес-ID", exact=True).fill("P0051")
                        page.get_by_role("button", name="Применить", exact=True).click()
                        page.wait_for_url("**/quality?**business_key=P0051*")
                        page.get_by_role("link", name="Поступление не сверено", exact=True).click()
                        assert page.get_by_role(
                            "heading", name="Поступление не сверено", exact=True
                        ).is_visible()
                        effects = (
                            page.get_by_role("heading", name="Решения по финансовым фактам")
                            .locator("xpath=ancestor::section[1]")
                            .inner_text()
                        )
                        assert "Включено" in effects and "Исключено" in effects
                        assert "R0102" in effects and "P0051" in effects
                        page.get_by_role("link", name="Импорты", exact=True).click()
                        assert page.get_by_role(
                            "heading", name="История импортов", exact=True
                        ).is_visible()
                        history = page.request.get(settings.auth.origin + "/api/v1/imports").json()
                        current = history["active_run_id"]
                        baseline = next(
                            (
                                run["run_id"]
                                for run in history["items"]
                                if run["run_id"] != current and run["status"] == "published"
                            ),
                            current,
                        )
                        page.get_by_role("link", name=current[:8], exact=True).click()
                        assert page.get_by_role(
                            "heading", name="Сравнение наборов", exact=True
                        ).is_visible()
                        page.get_by_text("Изменить базовый снимок", exact=True).click()
                        page.get_by_label("Базовый опубликованный run_id", exact=True).fill(
                            baseline
                        )
                        page.get_by_role("button", name="Сравнить", exact=True).click()
                        page.wait_for_url("**/imports/**?base_run_id=*")
                        assert "не доказывает удаление" in page.locator("main").inner_text()
                        page.get_by_role("link", name="Качество снимка →", exact=True).click()
                        page.locator("#quality-filters > summary").click()
                        page.get_by_text("Дополнительные фильтры", exact=True).click()
                        page.get_by_label("Код проблемы", exact=True).select_option(
                            "DIRECT_WITH_OTA"
                        )
                        page.get_by_role("button", name="Применить", exact=True).click()
                        page.wait_for_url("**/quality?**code=DIRECT_WITH_OTA*")
                        assert page.locator("[data-count=issues] strong").inner_text() == "30"
                        output = BASE_DIR / ".cache" / "e05-quality.png"
                        output.parent.mkdir(exist_ok=True)
                        page.screenshot(path=str(output))
                    page.locator(".user-menu > summary").click()
                    page.get_by_role("button", name="Выйти", exact=True).click()
                    page.wait_for_url("**/login")
                assert (
                    page.request.get(settings.auth.origin + "/api/v1/quality-issues").status == 401
                )
                print(
                    "E05 Chrome: unallocated admin rows/amounts/raw/details/filters/mobile/scope, viewer filters/pair/raw, independent cash/accrual, history/comparison, Direct count and logout passed."
                )
            finally:
                browser.close()
    finally:
        for username, _password in accounts:
            asyncio.run(remove_user(username))


if __name__ == "__main__":
    main()
