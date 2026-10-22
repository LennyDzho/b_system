"""Protect chart amounts, unknown commissions and bounded financial calendar aggregation."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from src.core.settings import BASE_DIR
from src.finance.policy import FinancialFact, aggregate, calculate
from src.finance.series import period_series
from src.services.imports.parser import canonicalize, capture
from tests.test_finance import EXPECTED, METRICS


@pytest.mark.parametrize("start,end,expected,bookings,unknown", EXPECTED)
def test_chart_periods_preserve_all_and_monthly_controls(
    start: date | None, end: date | None, expected: list[int], bookings: int, unknown: int
) -> None:
    """Compare both chart series to independent section-nine controls with exact Decimal sums."""
    facts = calculate(canonicalize(capture(BASE_DIR / "data")))
    start, end = start or date(2026, 9, 1), end or date(2026, 11, 5)
    selected = [f for f in facts if f.fact_date is None or start <= f.fact_date <= end]
    for comparison in (False, True):
        series = period_series(selected, start, end, comparison=comparison)
        for key, value in zip(METRICS, expected, strict=True):
            assert (
                sum(
                    (
                        Decimal(point["metrics"][key]["known_subtotal"])
                        for point in series["points"]
                    ),
                    Decimal(0),
                )
                == value
            )
        assert sum(p["metrics"]["management"]["unknown_count"] for p in series["points"]) == unknown
        assert series["points"][0]["date_from"] == start.isoformat()
        assert series["points"][-1]["date_to"] == end.isoformat()


def sample_fact() -> FinancialFact:
    """Build a small independent valid booking; artificial cases are not source CSV findings."""
    return FinancialFact(
        "reservations.csv",
        "booking",
        "villa",
        date(2026, 9, 30),
        "IDR",
        True,
        [2],
        gross=Decimal("100"),
        refund=Decimal("0"),
        ota=Decimal("10"),
        management=Decimal("20"),
    )


def test_chart_uses_each_financial_date_and_never_assigns_undated_records() -> None:
    """Keep cash and expense recognition separate across months; count undated exclusions once."""
    booking = sample_fact()
    expense = FinancialFact(
        "expenses.csv",
        "expense",
        "villa",
        date(2026, 10, 1),
        "IDR",
        True,
        [2],
        amount=Decimal("20"),
    )
    payment = FinancialFact(
        "payments.csv",
        "payment",
        "villa",
        date(2026, 10, 2),
        "IDR",
        True,
        [2],
        amount=Decimal("90"),
    )
    undated = replace(expense, business_key="undated", fact_date=None, included=False)
    series = period_series(
        [booking, expense, payment, undated], date(2026, 9, 30), date(2026, 10, 2), comparison=True
    )
    september, october = series["points"]
    assert september["metrics"]["gross"]["value"] == "100"
    assert september["metrics"]["cash"]["value"] == "0"
    assert october["metrics"]["gross"]["value"] == "0"
    assert october["metrics"]["cash"]["value"] == "90"
    assert october["metrics"]["owner_estimate"]["value"] == "-20"
    assert series["undated_count"] == 1
    assert sum(p["excluded_count"] for p in series["points"]) == 0


def test_unknown_fee_is_null_and_real_empty_day_is_zero() -> None:
    """Retain an unknown fee and its estimate; fill a no-activity day with known empty zeros."""
    booking = replace(sample_fact(), management=None, fee_reason="MISSING_FEE_RULE")
    series = period_series([booking], date(2026, 9, 30), date(2026, 10, 1))
    first, empty = series["points"]
    assert first["metrics"]["management"]["value"] is None
    assert first["metrics"]["management"]["unknown_count"] == 1
    assert not first["metrics"]["owner_estimate"]["is_complete"]
    assert first["metrics"]["owner_estimate"]["is_estimate"]
    assert first["metrics"]["owner_estimate"]["value"] == "90"
    assert empty["metrics"]["management"]["value"] == "0"
    assert empty["metrics"]["management"]["is_complete"]


@pytest.mark.parametrize(
    "start,end,granularity",
    [
        (date.min, date.max, "year"),
        (date(1900, 1, 1), date(2299, 12, 31), "year"),
        (date(2026, 1, 1), date(2027, 1, 1), "week"),
        (date(2026, 1, 1), date(2031, 1, 1), "month"),
        (date.max, date.max, "day"),
    ],
)
def test_long_and_boundary_periods_are_bounded(start: date, end: date, granularity: str) -> None:
    """Prevent daily expansion of arbitrary filters and avoid overflow at date.max."""
    series = period_series([], start, end)
    assert series["granularity"] == granularity
    assert len(series["points"]) <= 128
    assert series["points"][0]["date_from"] == start.isoformat()
    assert series["points"][-1]["date_to"] == end.isoformat()
    for previous, following in zip(series["points"], series["points"][1:]):
        assert (
            date.fromisoformat(following["date_from"]) - date.fromisoformat(previous["date_to"])
        ).days == 1


def test_excluded_money_does_not_enter_chart_values() -> None:
    """Include a dated bad row in coverage without accepting its amount into chart sums."""
    rejected = replace(sample_fact(), included=False, reasons=["INVALID_ARITHMETIC"])
    series = period_series([rejected], rejected.fact_date, rejected.fact_date)
    point = series["points"][0]
    assert point["excluded_count"] == 1
    assert point["metrics"] == aggregate([rejected])["metrics"]
    assert point["metrics"]["gross"]["value"] == "0"
    assert not point["metrics"]["gross"]["is_complete"]


def test_inverted_chart_range_is_rejected() -> None:
    """Reject invalid caller ranges instead of silently returning an empty financial series."""
    with pytest.raises(ValueError):
        period_series([], date(2026, 10, 1), date(2026, 9, 1))
