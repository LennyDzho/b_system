"""Exercise E02 against real PostgreSQL including migration rollback and CLI."""

import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text, update

from src.api.v1.auth.schemas import CreateUserData
from src.api.v1.auth.service import AuthService, token_digest
from src.app.__main__ import create_app
from src.core.settings import BASE_DIR, Settings, load_settings
from src.database.helper import DatabaseHelper
from src.database.models import User, UserPropertyAccess, UserSession
from src.database.repositories.auth import AuthRepo

pytestmark = pytest.mark.integration


@pytest.fixture
async def auth_database() -> AsyncIterator[tuple[Settings, DatabaseHelper]]:
    """Create, migrate, and finally drop a uniquely named local test database."""
    config = load_settings(env_file=None).model_copy(deep=True)
    name = "e02_test_" + secrets.token_hex(12)
    admin = DatabaseHelper(config.db.dsn())
    config.db.database = name
    helper = DatabaseHelper(config.db.dsn())
    created = False
    try:
        async with admin.async_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        async with helper.async_engine.begin() as connection:
            cfg = Config(str(BASE_DIR / "alembic.ini"))

            def migrate(sync_connection: object) -> None:
                """Use the real async connection through Alembic's sync facade."""
                cfg.attributes["connection"] = sync_connection
                command.upgrade(cfg, "head")

            await connection.run_sync(migrate)
        yield config, helper
    finally:
        await helper.dispose()
        try:
            if created:
                async with admin.async_engine.connect() as connection:
                    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                    await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


async def seed(
    helper: DatabaseHelper, config: Settings, username: str, role: str, ids: list[str]
) -> str:
    """Create a random test password through the same service as the CLI."""
    password = secrets.token_urlsafe(24)
    async with helper.session_factory() as session:
        await AuthService(AuthRepo(session), config.auth).create_user(
            CreateUserData(username=username, password=password, role=role, property_ids=ids)
        )
    return password


@pytest.mark.parametrize("https", [False, True])
async def test_login_scope_logout_and_revocation(
    auth_database: tuple[Settings, DatabaseHelper],
    https: bool,
) -> None:
    """Verify cookie issuance, current scopes, UI, rotation, and irrevocable logout."""
    config, helper = auth_database
    if https:
        config.auth.origin = "https://localhost:8000"
    config.app.api_key = "internal-service-key-not-for-browser"
    password = await seed(helper, config, "viewer", "viewer", ["V001", "V002"])
    application = create_app(config)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            assert (await client.get("/api/v1/auth/me")).status_code == 401
            assert (
                await client.get("/api/v1/auth/me", headers={"x-api-key": config.app.api_key})
            ).status_code == 401
            assert (await client.get("/account")).headers["location"] == "/login"
            assert (await client.get("/login")).status_code == 200
            credentials = {"username": "VIEWER", "password": password}
            response = await client.post("/api/v1/auth/login", json=credentials)
            assert response.status_code == 200
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Max-Age=28800" in cookie
            assert ("; Secure" in cookie) == https
            token = client.cookies["villa_session"]
            assert token not in response.text
            async with helper.session_factory() as session:
                row = await session.get(UserSession, token_digest(token))
                assert row is not None and row.token_hash != token
                user = await session.scalar(select(User))
                assert (
                    user.password_hash.startswith("$argon2id$")
                    and password not in user.password_hash
                )
            me = (await client.get("/api/v1/auth/me")).json()
            assert me["access"] == {"all_properties": False, "property_ids": ["V001", "V002"]}
            assert "hash" not in str(me)
            assert (await client.get("/api/v1/auth/me?property_id=V003")).status_code == 403
            assert (await client.get("/api/v1/auth/me?property_id=V001")).json()["access"][
                "property_ids"
            ] == ["V001"]
            page = await client.get("/account")
            assert page.status_code == 200 and "V001" in page.text and "logout-form" in page.text
            assert password not in page.text and token not in page.text
            assert config.app.api_key not in page.text
            assert (await client.get("/login")).headers["location"] == "/dashboard"
            assert (
                await client.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
            ).status_code == 403
            assert (await client.get("/api/v1/auth/me")).status_code == 200
            assert (await client.post("/api/v1/auth/login", json=credentials)).status_code == 200
            rotated = client.cookies["villa_session"]
            assert token != rotated
            assert (
                await client.get("/api/v1/auth/me", headers={"Cookie": f"villa_session={token}"})
            ).status_code == 401
            assert (await client.post("/api/v1/auth/logout")).status_code == 200
            assert "villa_session" not in client.cookies
            assert (
                await client.get("/api/v1/auth/me", headers={"Cookie": f"villa_session={rotated}"})
            ).status_code == 401


async def test_invalid_expired_inactive_and_current_assignments(
    auth_database: tuple[Settings, DatabaseHelper],
) -> None:
    """Reject bad credentials and immediately apply current database permissions."""
    config, helper = auth_database
    password = await seed(helper, config, "empty", "viewer", [])
    application = create_app(config)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url=config.auth.origin,
            headers={"Origin": config.auth.origin},
        ) as client:
            for username, attempt in [("empty", "wrong-password"), ("unknown", password)]:
                response = await client.post(
                    "/api/v1/auth/login", json={"username": username, "password": attempt}
                )
                assert response.status_code == 401 and attempt not in response.text
            assert (
                await client.get("/api/v1/auth/me", headers={"Cookie": "villa_session=forged"})
            ).status_code == 401
            await client.post(
                "/api/v1/auth/login", json={"username": "empty", "password": password}
            )
            assert (await client.get("/api/v1/auth/me")).json()["access"] == {
                "all_properties": False,
                "property_ids": [],
            }
            assert (await client.get("/api/v1/auth/me?property_id=V001")).status_code == 403
            assert "Объекты пока не назначены" in (await client.get("/account")).text
            async with helper.session_factory() as session:
                user = await session.scalar(select(User))
                session.add(UserPropertyAccess(user_id=user.id, property_id="V001"))
                await session.commit()
            assert (await client.get("/api/v1/auth/me?property_id=V001")).status_code == 200
            async with helper.session_factory() as session:
                await session.execute(update(User).values(role="admin"))
                await session.commit()
            assert (await client.get("/api/v1/auth/me")).json()["access"]["all_properties"] is True
            assert (await client.get("/api/v1/auth/me?property_id=EXTERNAL")).status_code == 200
            async with helper.session_factory() as session:
                await session.execute(
                    update(UserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await session.commit()
            assert (await client.get("/api/v1/auth/me")).status_code == 401
            await client.post(
                "/api/v1/auth/login", json={"username": "empty", "password": password}
            )
            async with helper.session_factory() as session:
                await session.execute(update(User).values(is_active=False))
                await session.commit()
            assert (await client.get("/api/v1/auth/me")).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login", json={"username": "empty", "password": password}
                )
            ).status_code == 401


async def test_real_cli_and_migration_rollback(
    auth_database: tuple[Settings, DatabaseHelper],
) -> None:
    """Run the actual CLI, reject duplicate accounts, and round-trip the real schema."""
    config, helper = auth_database
    environment = os.environ.copy()
    environment.update(DB_NAME=config.db.database, E02_TEST_PASSWORD=secrets.token_urlsafe(24))
    arguments = [
        sys.executable,
        "-m",
        "src.cli.create_user",
        "--username",
        "cli-viewer",
        "--role",
        "viewer",
        "--property-id",
        "V001",
        "--password-env",
        "E02_TEST_PASSWORD",
    ]
    for expected in (0, 1):
        result = await asyncio.to_thread(
            subprocess.run,
            arguments,
            env=environment,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        assert environment["E02_TEST_PASSWORD"] not in result.stdout + result.stderr
    async with helper.session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "cli-viewer"))
        assert user is not None and user.role == "viewer"
        assert (await AuthRepo(session).property_ids(user.id)) == frozenset({"V001"})
    for operation in [("downgrade", "base"), ("upgrade", "head"), ("upgrade", "head"), ("check",)]:
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", *operation],
            env=environment,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
