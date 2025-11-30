"""
Database helper initialization.
This module imports and exposes the main database helper instance.
"""

from .helper import DatabaseHelper
from core import settings


"""Initialize the DatabaseHelper with settings from the core configuration."""
db_helper: DatabaseHelper = DatabaseHelper(
    settings.db.dsn(),
    echo=False,
    echo_pool=False,
    pool_size=10,
    max_overflow=50,
)

__all__ = ["db_helper"]
