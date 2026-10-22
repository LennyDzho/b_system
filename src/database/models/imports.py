"""Persist immutable attempt metadata and the single active snapshot pointer."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.database.models.base import Base


class ImportRun(Base):
    """Journal a captured dataset and its terminal publication outcome."""

    __tablename__ = "import_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','published','failed')", name="ck_import_runs_status"),
    )

    run_id: Mapped[UUID] = mapped_column(primary_key=True)
    dataset_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    pipeline_version: Mapped[str] = mapped_column(String(32))
    calculation_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_run_id: Mapped[UUID | None] = mapped_column(ForeignKey("import_runs.run_id"))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(128))


class DatasetState(Base):
    """Select one published snapshot; no financial calculation is implied."""

    __tablename__ = "dataset_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_dataset_state_singleton"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    active_run_id: Mapped[UUID] = mapped_column(ForeignKey("import_runs.run_id"))
