"""Group authorized financial facts into bounded periods using the shared Decimal policy."""

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from src.finance.policy import FinancialFact, aggregate

Granularity = Literal["day", "week", "month", "year"]


def bucket_start(value: date, granularity: Granularity, step: int) -> date:
    """Locate a calendar bucket; weeks start Monday and multi-year groups start at year one."""
    if granularity == "day":
        return value
    if granularity == "week":
        return value - timedelta(days=value.weekday())
    if granularity == "month":
        return value.replace(day=1)
    return date((value.year - 1) // step * step + 1, 1, 1)


def next_bucket(value: date, granularity: Granularity, step: int) -> date | None:
    """Advance without overflowing Python's supported inclusive date range."""
    if granularity in {"day", "week"}:
        days = 1 if granularity == "day" else 7
        return value + timedelta(days=days) if (date.max - value).days >= days else None
    if granularity == "month":
        if value.month < 12:
            return value.replace(month=value.month + 1)
        return date(value.year + 1, 1, 1) if value.year < date.max.year else None
    return date(value.year + step, 1, 1) if value.year + step <= date.max.year else None


def period_series(
    facts: list[FinancialFact], start: date, end: date, *, comparison: bool = False
) -> dict[str, Any]:
    """Aggregate IDR facts by their own recognition dates without recalculating rules.

    Callers provide the same authorized, snapshot-bound facts used for dashboard totals.
    Decimal values, rounding, exclusions, NULL fees and estimates come from aggregate().
    Empty calendar buckets contain proven empty-selection zeros. Undated facts stay
    outside buckets and are counted once as a series limitation, never assigned a date.
    Adaptive calendar intervals keep even date.min..date.max below 128 buckets.
    """
    if start > end:
        raise ValueError("Series start must not follow its end")
    days = (end - start).days
    granularity: Granularity = (
        "month"
        if comparison and days <= 3660
        else "day"
        if not comparison and days <= 92
        else "week"
        if not comparison and days <= 730
        else "month"
        if days <= 3660
        else "year"
    )
    step = max(1, (end.year - start.year + 120) // 120) if granularity == "year" else 1
    groups: dict[date, list[FinancialFact]] = defaultdict(list)
    for fact in facts:
        if fact.fact_date is not None and start <= fact.fact_date <= end:
            groups[bucket_start(fact.fact_date, granularity, step)].append(fact)
    points = []
    current = bucket_start(start, granularity, step)
    while current <= end:
        following = next_bucket(current, granularity, step)
        last = min(end, following - timedelta(days=1)) if following else end
        totals = aggregate(groups.get(current, []))
        points.append(
            {
                "date_from": max(start, current).isoformat(),
                "date_to": last.isoformat(),
                "metrics": totals["metrics"],
                "excluded_count": sum(counts["excluded"] for counts in totals["coverage"].values()),
            }
        )
        if following is None or following > end:
            break
        current = following
    return {
        "granularity": granularity,
        "step": step,
        "points": points,
        "undated_count": sum(fact.fact_date is None for fact in facts),
    }
