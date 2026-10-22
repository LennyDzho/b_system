"""Define scoped diagnostic filters and counters independently of HTTP adapters."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


class QualityFilters(BaseModel):
    """Validate filters independently of API/UI while preserving explicit empty scopes."""

    source: str | None = Field(default=None, max_length=64)
    code: str | None = Field(default=None, max_length=64)
    action: str | None = Field(default=None, max_length=64)
    severity: Literal["info", "warning", "error"] | None = None
    business_key: str | None = Field(default=None, max_length=1024)
    date_from: date | None = None
    date_to: date | None = None
    section: Literal["all", "period", "limitations"] = "all"
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class QualityResponse(BaseModel):
    """Document scoped diagnostic counts, independent limitations and paginated records."""

    state: Literal["ready", "no_snapshot"]
    run_id: str | None = None
    pipeline_version: str | None = None
    calculation_version: str | None = None
    total: int = 0
    limit: int = 25
    offset: int = 0
    scope_counts: dict[str, Any] = Field(default_factory=dict)
    filtered_counts: dict[str, Any] = Field(default_factory=dict)
    facets: dict[str, list[str]] = Field(default_factory=dict)
    properties: list[dict[str, Any]] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
