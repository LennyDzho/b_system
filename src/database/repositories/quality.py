"""Read the administrative import journal without mutating historical attempts."""

from sqlalchemy import select

from src.database.models import ImportRun
from src.database.repositories.base import BaseRepo


class HistoryRepo(BaseRepo):
    """Keep PostgreSQL journal reads separate from diagnostic orchestration."""

    async def runs(self) -> list[ImportRun]:
        """Return a stable ordered small-prototype history for explicit pagination."""
        return list(
            (
                await self.session.scalars(
                    select(ImportRun).order_by(ImportRun.created_at.desc(), ImportRun.run_id)
                )
            ).all()
        )
