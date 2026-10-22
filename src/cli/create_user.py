"""Create a local account using a hidden prompt or a named environment variable."""

import argparse
import asyncio
import getpass
import os
from collections.abc import Sequence

from pydantic import ValidationError

from src.api.v1.auth.schemas import CreateUserData
from src.api.v1.auth.service import AuthService
from src.core.infra.exceptions import AuthError
from src.core.settings import settings
from src.database.helper import DatabaseHelper
from src.database.repositories.auth import AuthRepo


async def create(data: CreateUserData) -> None:
    """Use the shared service and always release the CLI connection pool."""
    helper = DatabaseHelper(settings.db.dsn(), timeout=settings.db.timeout)
    try:
        async with helper.session_factory() as session:
            user = await AuthService(AuthRepo(session), settings.auth).create_user(data)
            print(f"Пользователь {user.username} создан; роль: {user.role}.")
    finally:
        await helper.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Validate public arguments and report failures without printing secrets."""
    parser = argparse.ArgumentParser(description="Создание локального пользователя")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=["admin", "viewer"])
    parser.add_argument("--property-id", action="append", default=[])
    parser.add_argument("--password-env", help="Имя переменной окружения с паролем")
    args = parser.parse_args(argv)
    password = (
        os.environ.get(args.password_env) if args.password_env else getpass.getpass("Пароль: ")
    )
    try:
        data = CreateUserData(
            username=args.username, role=args.role, password=password, property_ids=args.property_id
        )
        asyncio.run(create(data))
    except ValidationError:
        print("Некорректные параметры: логин 3–64 символа, пароль 12–1024, ID объекта 1–64.")
        return 2
    except AuthError as exception:
        print(exception.detail)
        return 1
    except Exception as exception:
        print(
            f"Не удалось создать пользователя ({type(exception).__name__}). Проверьте БД и миграции."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
