"""Verify real dataset observations and synthetic quality edges without changing financial policy."""

from copy import deepcopy
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest

from src.core.settings import BASE_DIR
from src.finance.policy import aggregate, calculate
from src.quality.diagnostics import diagnose, issue_counts
from src.services.imports.parser import canonicalize, capture
from src.services.imports.records import Snapshot
from tests.import_helpers import write_sources
from tests.test_finance import fixture_rows


def calculated(directory: Path) -> Snapshot:
    """Use the actual shared canonicalization and monetary functions before diagnostics."""
    snapshot = canonicalize(capture(directory))
    snapshot.financial_facts = calculate(snapshot)
    return snapshot


def test_supplied_quality_observations_preserve_every_financial_fact() -> None:
    """Find exactly 73 pairs/87 bookings/17 properties and 30 Direct OTA warnings."""
    snapshot = calculated(BASE_DIR / "data")
    before = [asdict(f) for f in snapshot.financial_facts]
    diagnose(snapshot)
    assert [asdict(f) for f in snapshot.financial_facts] == before
    overlap = [i for i in snapshot.issues if i.code == "OCCUPANCY_OVERLAP"]
    assert len(overlap) == 73
    assert len({i.property_id for i in overlap}) == 17
    assert issue_counts(overlap)["affected_entities"] == 87
    direct = [i for i in snapshot.issues if i.code == "DIRECT_WITH_OTA"]
    assert len(direct) == len({i.business_key for i in direct}) == 30
    assert all(i.effects["facts"][0]["included"] for i in direct)
    assert not any(
        i.code
        in {
            "COMMISSION_RULE_GAP",
            "COMMISSION_RULE_OVERLAP",
            "PAYMENT_AMOUNT_DIFFERENCE",
            "PAYMENT_BOOKING_CURRENCY",
        }
        for i in snapshot.issues
    )
    assert sum(i.code == "PAYMENT_NOT_EXPORTED" for i in snapshot.issues) == 75
    assert sum(i.code == "ZERO_RECEIVED_PAYMENT" for i in snapshot.issues) == 7
    duplicates = [i for i in snapshot.issues if i.code == "duplicate"]
    assert all(i.inclusion == "excluded_raw" for i in duplicates)
    assert all(i.effects["facts"][0]["included"] for i in duplicates)
    for month in (None, 9, 10, 11):
        old = [f for f in snapshot.financial_facts if month is None or f.fact_date.month == month]
        assert aggregate(old)["metrics"]["owner_estimate"]["known_subtotal"] in {
            "785128468.00",
            "402219283.00",
            "366706078.00",
            "16203107.00",
        }


@pytest.mark.parametrize("second_start,expected", [("2026-09-03", 0), ("2026-09-02", 1)])
def test_stay_boundaries_before_financial_exclusion(
    tmp_path: Path, second_start: str, expected: int
) -> None:
    """Touching stays do not overlap; invalid financial values do not erase occupancy."""
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"].append(
        {
            **rows["reservations.csv"][0],
            "reservation_id": "booking-2",
            "check_in": second_start,
            "check_out": "2026-09-05",
            "currency": "USD",
            "ota_fee": "-1",
        }
    )
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    overlaps = [i for i in snapshot.issues if i.code == "OCCUPANCY_OVERLAP"]
    assert len(overlaps) == expected
    if overlaps:
        assert overlaps[0].period_start == overlaps[0].period_end == date(2026, 9, 2)
        assert issue_counts(overlaps)["affected_entities"] == 2
        assert any(not fact["included"] for fact in overlaps[0].effects["facts"])


@pytest.mark.parametrize(
    "start,code",
    [
        ("2026-09-03", "COMMISSION_RULE_OVERLAP"),
        ("2026-09-04", None),
        ("2026-09-06", "COMMISSION_RULE_GAP"),
    ],
)
def test_rule_inclusive_overlap_adjacent_and_internal_gap(
    tmp_path: Path, start: str, code: str | None
) -> None:
    """Synthetic rule defects are not claimed as findings in supplied source CSVs."""
    rows = fixture_rows(tmp_path)
    rows["commission_rules.csv"][0].update(effective_from="2026-09-01", effective_to="2026-09-03")
    rows["commission_rules.csv"].append(
        {**rows["commission_rules.csv"][0], "effective_from": start, "effective_to": "2026-09-10"}
    )
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    rules = [
        i for i in snapshot.issues if i.code in {"COMMISSION_RULE_OVERLAP", "COMMISSION_RULE_GAP"}
    ]
    assert [i.code for i in rules] == ([code] if code else [])
    assert all(i.is_global and i.period_start is None for i in rules)


def test_payment_comparison_aggregates_multiple_ids_and_links_invalid_accrual(
    tmp_path: Path,
) -> None:
    """Explain receipt differences with shared financial decisions without multiplying accrual."""
    rows = fixture_rows(tmp_path)
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    difference = next(i for i in snapshot.issues if i.code == "PAYMENT_AMOUNT_DIFFERENCE")
    assert difference.actual_values["expected_after_ota"] == "850.00"
    assert difference.actual_values["registered_cash"] == "200.00"
    assert len(difference.related_entities) == 2
    rows["payments.csv"][0]["amount"] = "300"
    rows["payments.csv"][1]["amount"] = "550"
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    assert not any(i.code == "PAYMENT_AMOUNT_DIFFERENCE" for i in snapshot.issues)
    rows["reservations.csv"][0]["ota_fee"] = "-1"
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    unreconciled = [i for i in snapshot.issues if i.code == "UNRECONCILED_PAYMENT"]
    assert len(unreconciled) == 2 and all(
        i.related_entities[0]["business_key"] == "booking-1" for i in unreconciled
    )
    assert all(
        any(f["source"] == "payments.csv" and f["included"] for f in i.effects["facts"])
        for i in unreconciled
    )


def test_latest_versions_duplicates_and_undated_problems(tmp_path: Path) -> None:
    """Only canonical stays generate pairs, while invalid-date provenance remains a limitation."""
    rows = fixture_rows(tmp_path)
    original = deepcopy(rows["reservations.csv"][0])
    latest = {**original, "updated_at": "2026-09-02", "check_out": "bad-date"}
    rows["reservations.csv"] = [original, original, latest]
    rows["commission_rules.csv"][0]["management_fee_rate"] = "2"
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    assert not any(i.code == "OCCUPANCY_OVERLAP" for i in snapshot.issues)
    assert any(i.period_start is None and i.source == "reservations.csv" for i in snapshot.issues)
    assert any(i.code == "INVALID_COMMISSION_RULE" for i in snapshot.issues)
    assert issue_counts(snapshot.issues)["discarded_raw_rows"] >= 1


@pytest.mark.parametrize("checkout", ["1800-01-03", "2300-01-03", "20260903"])
def test_diagnostic_dates_obey_shared_source_storage_contract(
    tmp_path: Path, checkout: str
) -> None:
    """Invalid source dates remain undated limitations rather than unrepresentable CH dates."""
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0]["check_out"] = checkout
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    assert all(i.period_start is None for i in snapshot.issues if i.source == "reservations.csv")


def test_missing_id_counts_one_entity_across_base_and_financial_issues(tmp_path: Path) -> None:
    """Use financial lineage to unify the raw missing ID and generated internal key."""
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0]["reservation_id"] = ""
    write_sources(tmp_path, rows)
    snapshot = calculated(tmp_path)
    diagnose(snapshot)
    counts = issue_counts([i for i in snapshot.issues if i.source == "reservations.csv"])
    assert counts["issues"] > 1 and counts["affected_rows"] == counts["affected_entities"] == 1
