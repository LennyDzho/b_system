"""Import five trusted local CSV files or inspect the persisted import journal."""

import argparse
import asyncio
import getpass
from collections.abc import Sequence
from pathlib import Path

from src.analytics.helper import ClickHouseHelper
from src.analytics.imports import AnalyticsImportRepo
from src.core.infra.exceptions import ImportBusy, ImportFailed, PublicationUnknown
from src.core.settings import BASE_DIR, settings
from src.database.helper import DatabaseHelper
from src.services.imports.records import json_text
from src.services.imports.service import ImportService


async def execute(directory: Path, actor: str, history: bool) -> None:
    """Use the shared import service and always release both database resources."""
    database = DatabaseHelper(settings.db.dsn(), timeout=settings.db.timeout)
    analytics = ClickHouseHelper(settings.clickhouse)
    try:
        service = ImportService(database, AnalyticsImportRepo(analytics))
        result = await service.history() if history else await service.run(directory, actor)
        print(json_text(result))
    finally:
        try:
            await analytics.dispose()
        finally:
            await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """Report definite failure, concurrent lease ownership, and unknown commit distinctly."""
    parser = argparse.ArgumentParser(
        description="Импорт полного снимка с финансовым расчётом и диагностикой"
    )
    parser.add_argument("--data-dir", type=Path, default=settings.app.data_dir)
    parser.add_argument(
        "--actor",
        default="local-cli:" + getpass.getuser(),
        help="Метка доверенного локального оператора",
    )
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args(argv)
    directory = args.data_dir if args.data_dir.is_absolute() else BASE_DIR / args.data_dir
    try:
        asyncio.run(execute(directory, args.actor, args.history))
    except PublicationUnknown as error:
        print(str(error))
        return 2
    except ImportBusy as error:
        print(str(error))
        return 3
    except ImportFailed as error:
        print(str(error))
        return 1
    except Exception as error:
        print(
            f"Импорт/журнал недоступен ({type(error).__name__}); успешная публикация не подтверждена"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
