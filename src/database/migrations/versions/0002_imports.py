"""Add the import-attempt journal and singleton published snapshot pointer."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_imports"
down_revision: str | None = "0001_auth"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create E03 metadata without prepopulating a snapshot or financial values."""
    op.create_table(
        "import_runs",
        sa.Column("run_id", sa.Uuid(), primary_key=True),
        sa.Column("dataset_hash", sa.String(64), nullable=True),
        sa.Column("pipeline_version", sa.String(32), nullable=False),
        sa.Column("calculation_version", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("previous_run_id", sa.Uuid(), sa.ForeignKey("import_runs.run_id"), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','published','failed')", name="ck_import_runs_status"
        ),
    )
    op.create_index("ix_import_runs_dataset_hash", "import_runs", ["dataset_hash"])
    op.create_table(
        "dataset_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("active_run_id", sa.Uuid(), sa.ForeignKey("import_runs.run_id"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_dataset_state_singleton"),
    )


def downgrade() -> None:
    """Drop only E03 metadata; source data in ClickHouse is managed separately."""
    op.drop_table("dataset_state")
    op.drop_table("import_runs")
