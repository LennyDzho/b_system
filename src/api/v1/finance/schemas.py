"""Describe exact monetary metrics and authorized snapshot responses."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class MoneyMetric(BaseModel):
    """Document exact IDR subtotal, unknown totals and domain-specific completeness."""

    value: str | None
    known_subtotal: str
    unknown_count: int
    is_complete: bool
    is_estimate: bool
    reasons: dict[str, int]


class ChartPeriod(BaseModel):
    """Expose exact per-period amounts, unknown fees and exclusions from shared aggregation."""

    date_from: str
    date_to: str
    metrics: dict[str, MoneyMetric]
    excluded_count: int


class ChartSeries(BaseModel):
    """Bound a calendar series and describe undated records without inventing dates."""

    granularity: Literal["day", "week", "month", "year"]
    step: int = 1
    points: list[ChartPeriod]
    undated_count: int = 0


class DashboardCharts(BaseModel):
    """Carry chart series computed from the same authorized snapshot as all dashboard totals."""

    trend: ChartSeries
    cash_accrual: ChartSeries


class DashboardResponse(BaseModel):
    """Expose snapshot identity, exact metrics and independently scoped coverage."""

    state: Literal["ready", "empty", "no_snapshot", "not_calculated"]
    run_id: str | None = None
    calculation_version: str | None = None
    pipeline_version: str | None = None
    imported_at: str | None = None
    currency: str = "IDR"
    date_from: str | None = None
    date_to: str | None = None
    scope: list[str] = Field(default_factory=list)
    metrics: dict[str, MoneyMetric] = Field(default_factory=dict)
    coverage: dict[str, dict[str, int]] = Field(default_factory=dict)
    properties: list[dict[str, Any]] = Field(default_factory=list)
    records: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    reasons: dict[str, int] = Field(default_factory=dict)
    unknown_fees: int = 0
    unreconciled_cash: str = "0"
    unreconciled_count: int = 0
    positive_payments: int = 0
    unallocated: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Только admin без фильтра объекта: группы исключённых нераспределённых фактов по источнику/валюте с count, observed_amount и records (ID, финансовая дата, исходные суммы, причины и source_rows). Тот же run_id и период, что у dashboard; отсутствующая сумма остаётся NULL.",
    )
    snapshot_limitations: list[str] = Field(default_factory=list)
    charts: DashboardCharts | None = Field(
        default=None,
        description="Серверные ряды того же run_id и scope; NULL при отсутствии рассчитанных датированных данных. Деньги — точные строки, неизвестная комиссия — NULL, оценка явно помечена.",
    )
