"""
Base repository providing common database operations.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import Base


class BaseRepo:
    """
    Base repository providing common database operations.
    """

    def __init__(self, session: AsyncSession) -> None:
        """
        Initializes the BaseRepo with a database session.
        """
        self.session = session

    def add(self, obj: Base) -> None:
        """
        Adds an object to the session.
        """
        self.session.add(obj)

    async def delete(self, obj: Base) -> None:
        """
        Deletes an object from the session.
        """
        await self.session.delete(obj)

    async def commit(self) -> None:
        """
        Commits the current transaction.
        """
        await self.session.commit()

    async def rollback(self) -> None:
        """
        Rolls back the current transaction.
        """
        await self.session.rollback()
