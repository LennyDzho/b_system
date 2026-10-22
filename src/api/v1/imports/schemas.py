"""Describe administrator import filters, immutable attempts and business comparisons."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from src.core.infra.schemas import ErrorResponse

ImportStatus = Literal["running", "failed", "published"]


class ImportFilters(BaseModel):
    """Validate journal pagination and status equally for API and HTML."""

    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    status: ImportStatus | None = None


class ImportRunResponse(BaseModel):
    """Expose stored attempt metadata only after administrator authorization."""

    run_id: str
    status: ImportStatus
    actor: str
    created_at: str
    finished_at: str | None
    pipeline_version: str
    calculation_version: str | None
    dataset_hash: str | None
    manifest: dict[str, Any]
    summary: dict[str, Any]
    error_code: str | None
    previous_run_id: str | None


class ImportHistoryResponse(BaseModel):
    """Return explicit pagination and the active pointer without exposing audit to viewers."""

    active_run_id: str | None
    total: int
    limit: int
    offset: int
    items: list[ImportRunResponse]


class ImportComparisonResponse(BaseModel):
    """Compare business hashes without treating absence as source-system deletion."""

    run: ImportRunResponse
    base_run_id: str | None
    comparison: dict[str, dict[str, list[str]]] | None
    notice: str


class ImportExecutionResponse(BaseModel):
    """Report a verified publication or reuse without confusing the run and active snapshot."""

    run_id: str
    active_run_id: str | None
    status: Literal["published"]
    reused: bool
    pipeline_version: str
    calculation_version: str | None
    summary: dict[str, Any]
    message: str


class ImportProblemResponse(ErrorResponse):
    """Distinguish rejection, busy, known failure and uncertain publication for the UI."""

    outcome: Literal["invalid", "busy", "failed", "unknown", "unavailable"]
    run_id: str | None = None
