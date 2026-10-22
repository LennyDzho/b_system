"""Run ordered ClickHouse SQL migrations and verify their immutable checksums."""

import asyncio
import hashlib
import logging
import re
from pathlib import Path

from src.analytics.helper import ClickHouseHelper
from src.core.log_setup import setup_logging
from src.core.settings import settings

logger = logging.getLogger(__name__)
SQL_DIR = Path(__file__).parent / "sql"


async def migrate(helper: ClickHouseHelper, directory: Path = SQL_DIR) -> list[str]:
    """Apply single-statement idempotent DDL files, checking immutable history.

    ClickHouse DDL and history inserts are not one transaction. A failed or
    uncertain attempt can be retried because every SQL file must be idempotent.
    Run this administrative command from one process before application rollout.
    """
    files = sorted(directory.glob("*.sql"))
    if not files or files[0].name != "0001_schema_migrations.sql":
        raise RuntimeError("The migration-history bootstrap SQL is missing")
    versions: set[str] = set()
    for path in files:
        if not re.fullmatch(r"[0-9]{4}_[a-z0-9_]+\.sql", path.name):
            raise RuntimeError(f"Invalid migration filename: {path.name}")
        version = path.name[:4]
        if version in versions:
            raise RuntimeError(f"Duplicate migration version: {version}")
        versions.add(version)

    client = await helper.get_client()
    # Universal newlines keep checksums stable between Windows and Linux checkouts.
    sources = {path.name: path.read_text(encoding="utf-8") for path in files}
    checksums = {
        name: hashlib.sha256(sql.encode("utf-8")).hexdigest() for name, sql in sources.items()
    }
    if not await client.command("EXISTS TABLE schema_migrations"):
        await client.command(sources[files[0].name])
    result = await client.query("SELECT version, checksum FROM schema_migrations")
    history = {
        name: checksum.decode("ascii") if isinstance(checksum, bytes) else checksum
        for name, checksum in result.result_rows
    }
    if len(history) != len(result.result_rows):
        raise RuntimeError("Duplicate migration history; inspect the previous migration attempt")
    available = {path.name for path in files}
    if history.keys() - available:
        raise RuntimeError("Applied migrations are missing from this checkout")
    applied: list[str] = []
    for path in files:
        if path.name in history and history[path.name] != checksums[path.name]:
            raise RuntimeError(f"Applied migration was changed: {path.name}")
    for path in files:
        if path.name in history:
            continue
        if any(version > path.name for version in history):
            raise RuntimeError("A migration was added before an applied version")
        await client.command(sources[path.name])
        await client.insert(
            "schema_migrations",
            [[path.name, checksums[path.name]]],
            column_names=["version", "checksum"],
        )
        applied.append(path.name)
        logger.info("Applied ClickHouse migration %s", path.name)
    return applied


async def main() -> None:
    """Run the command with finite DB timeouts and unconditional client cleanup."""
    setup_logging()
    helper = ClickHouseHelper(settings.clickhouse)
    try:
        applied = await migrate(helper)
        logger.info("ClickHouse migrations complete: %s applied.", len(applied))
    finally:
        await helper.dispose()


if __name__ == "__main__":
    asyncio.run(main())
