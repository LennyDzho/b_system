"""Run an explicit real-browser E02 smoke test with a temporary local account."""

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from sqlalchemy import delete

from src.core.settings import BASE_DIR, settings
from src.database.helper import DatabaseHelper
from src.database.models import User


async def remove_user(username: str) -> None:
    """Remove only the random account created by this smoke test and its sessions."""
    helper = DatabaseHelper(settings.db.dsn())
    try:
        async with helper.session_factory() as session:
            await session.execute(delete(User).where(User.username == username))
            await session.commit()
    finally:
        await helper.dispose()


def main() -> None:
    """Verify the actual HTML/JavaScript/cookie flow without printing credentials."""
    username = "browser-" + secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    environment = os.environ.copy()
    environment["E02_BROWSER_PASSWORD"] = password
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
            "V001",
            "--password-env",
            "E02_BROWSER_PASSWORD",
        ],
        cwd=BASE_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Could not create the temporary browser account")
    try:
        with sync_playwright() as playwright:
            chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
            browser = playwright.chromium.launch(
                headless=True, **({"executable_path": str(chrome)} if chrome.is_file() else {})
            )
            try:
                page = browser.new_page()
                page.goto(settings.auth.origin + "/")
                page.wait_for_url("**/login")
                page.get_by_label("Логин").fill(username)
                page.get_by_label("Пароль").fill("incorrect-password")
                page.get_by_role("button", name="Войти").click()
                page.get_by_role("alert").filter(has_text="Неверный логин или пароль").wait_for()
                page.get_by_label("Пароль").fill(password)
                page.get_by_role("button", name="Войти").click()
                page.wait_for_url("**/dashboard")
                assert page.get_by_role("heading", name="Финансы портфеля", exact=True).is_visible()
                page.goto(settings.auth.origin + "/account")
                assert page.get_by_role("heading", name="Учётная запись").is_visible()
                assert page.get_by_text("V001", exact=True).is_visible()
                me = page.request.get(settings.auth.origin + "/api/v1/auth/me")
                assert me.status == 200 and me.json()["access"]["property_ids"] == ["V001"]
                token = next(
                    cookie["value"]
                    for cookie in page.context.cookies()
                    if cookie["name"] == "villa_session"
                )
                page.locator(".user-menu > summary").click()
                page.get_by_role("button", name="Выйти").click()
                page.wait_for_url("**/login")
                assert page.request.get(settings.auth.origin + "/api/v1/auth/me").status == 401
                replay = page.request.get(
                    settings.auth.origin + "/api/v1/auth/me",
                    headers={"Cookie": "villa_session=" + token},
                )
                assert replay.status == 401
                print(
                    "Browser E02: invalid password, login, account, scope, logout, replay rejection passed."
                )
            finally:
                browser.close()
    finally:
        asyncio.run(remove_user(username))


if __name__ == "__main__":
    main()
