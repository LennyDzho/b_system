"""
Base class for all ORM models.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Base class for all ORM models.
    """

    __abstract__ = True
