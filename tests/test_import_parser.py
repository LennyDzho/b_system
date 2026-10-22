"""Protect canonical source versions, provenance, exact types, and schema failures."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.settings import BASE_DIR
from src.services.imports.parser import canonicalize, capture, parse_decimal
from tests.import_helpers import write_sources


def test_supplied_sources_counts_and_duplicate_lineage() -> None:
    """Reproduce source counts without any financial admission or sum assertions."""
    snapshot = canonicalize(capture(BASE_DIR / "data"))
    assert not snapshot.errors
    assert snapshot.counts() == {
        "raw": 357,
        "issues": 5,
        "properties.csv": 20,
        "reservations.csv": 150,
        "expenses.csv": 90,
        "payments.csv": 75,
        "commission_rules.csv": 20,
    }
    duplicates = [issue for issue in snapshot.issues if issue.code == "duplicate"]
    assert {issue.business_key for issue in duplicates} == {"R0015", "P0075"}
    for issue in duplicates:
        row = next(
            row
            for row in snapshot.canonical
            if row.source == issue.source and row.business_key == issue.business_key
        )
        assert len(row.source_rows) == 2 and issue.row_number in row.source_rows
        assert issue.inclusion == "not_calculated"
    assert min(row.row_number for row in snapshot.raw) == 2


@pytest.mark.parametrize(
    "scenario",
    [
        "latest",
        "latest_invalid",
        "latest_financially_suspect",
        "tie",
        "invalid_timestamp",
        "offsets",
    ],
)
def test_reservation_versions_never_fall_back_or_use_row_order(
    tmp_path: Path, scenario: str
) -> None:
    """Resolve only a unique latest version and preserve invalid current values."""
    write_sources(tmp_path)
    original = next(
        row.payload for row in capture(tmp_path).raw if row.source == "reservations.csv"
    )
    newer = deepcopy(original)
    newer.update(updated_at="2026-09-02", gross_booking_value="2000")
    if scenario == "latest_invalid":
        newer["gross_booking_value"] = "not-money"
    if scenario == "latest_financially_suspect":
        newer.update(ota_fee="-5", refund_amount="9999")
    if scenario == "tie":
        newer["updated_at"] = original["updated_at"]
    if scenario == "invalid_timestamp":
        newer["updated_at"] = "unknown"
    if scenario == "offsets":
        original["updated_at"] = "2026-09-02T03:00:00+03:00"
        newer["updated_at"] = "2026-09-02T00:00:01Z"
    for versions in ([original, newer], [newer, original]):
        write_sources(tmp_path, {"reservations.csv": versions})
        snapshot = canonicalize(capture(tmp_path))
        record = next(row for row in snapshot.canonical if row.source == "reservations.csv")
        assert len(record.source_rows) == 2
        if scenario in ("tie", "invalid_timestamp"):
            assert record.type_status == "conflict" and record.typed == {}
        elif scenario == "latest_invalid":
            assert record.type_status == "invalid" and record.typed["gross_booking_value"] is None
            assert record.payload["gross_booking_value"] == "not-money"
        else:
            assert record.typed["gross_booking_value"] == Decimal("2000")
            if scenario == "latest_financially_suspect":
                assert record.typed["ota_fee"] == Decimal("-5")
                assert record.typed["refund_amount"] == Decimal("9999")


def test_unversioned_conflict_and_distinct_payments(tmp_path: Path) -> None:
    """Keep distinct payment IDs for one reservation and flag same-ID payload conflicts."""
    write_sources(tmp_path)
    baseline = capture(tmp_path)
    payments = [row.payload for row in baseline.raw if row.source == "payments.csv"]
    assert (
        len([row for row in canonicalize(baseline).canonical if row.source == "payments.csv"]) == 2
    )
    conflicting = deepcopy(payments[0])
    conflicting["amount"] = "250"
    write_sources(tmp_path, {"payments.csv": payments + [conflicting]})
    snapshot = canonicalize(capture(tmp_path))
    assert (
        next(row for row in snapshot.canonical if row.business_key == "payment-1").type_status
        == "conflict"
    )


@pytest.mark.parametrize(
    "value", ["NaN", "Infinity", "1000000000000000000", "1e1000000", "1.001", "not-money"]
)
def test_decimal_storage_does_not_round_or_create_unknown_zeros(value: str) -> None:
    """Reject nonfinite/overflow/inexact values while keeping their source representation."""
    with pytest.raises((ValueError, ArithmeticError)):
        parse_decimal(value, 2, 20)


def test_bad_types_and_unknown_references_stay_visible(tmp_path: Path) -> None:
    """Publish partial canonical entities with nullable types and separate reference status."""
    write_sources(tmp_path)
    reservation = next(
        row.payload for row in capture(tmp_path).raw if row.source == "reservations.csv"
    )
    reservation.update(
        property_id="unknown", check_out="2026-02-30", gross_booking_value="bad", status="mystery"
    )
    write_sources(tmp_path, {"reservations.csv": [reservation]})
    snapshot = canonicalize(capture(tmp_path))
    record = next(row for row in snapshot.canonical if row.source == "reservations.csv")
    assert record.type_status == "invalid" and record.reference_status == "unresolved"
    assert record.typed["gross_booking_value"] is None
    assert {"invalid_type", "unknown_property", "unknown_status"} <= {
        issue.code for issue in snapshot.issues
    }
    assert (
        next(issue for issue in snapshot.issues if issue.code == "unknown_property").property_id
        is None
    )


@pytest.mark.parametrize("failure", ["missing", "header", "encoding", "quotes"])
def test_preflight_retains_partial_manifest(tmp_path: Path, failure: str) -> None:
    """Capture usable file metadata even when a complete snapshot cannot be processed."""
    write_sources(tmp_path)
    path = tmp_path / "expenses.csv"
    if failure == "missing":
        path.unlink()
    else:
        content = {
            "header": b"wrong,header\n1,2\n",
            "encoding": b"\xff\xfe\xff",
            "quotes": b'expense_id,property_id,expense_date,category,amount,currency,payment_status\n"unterminated',
        }[failure]
        path.write_bytes(content)
    snapshot = capture(tmp_path)
    assert snapshot.errors and len(snapshot.manifest) == 5
    assert snapshot.manifest["properties.csv"]["rows"] == 1
    if failure == "missing":
        assert snapshot.dataset_hash() is None


def test_row_width_is_a_partial_row_issue_and_capture_freezes_bytes(tmp_path: Path) -> None:
    """A malformed data row stays raw, and later disk edits do not change captured data."""
    write_sources(tmp_path)
    path = tmp_path / "expenses.csv"
    path.write_bytes(path.read_bytes() + b"broken-id,too-few\n")
    snapshot = capture(tmp_path)
    frozen_hash = snapshot.dataset_hash()
    path.write_bytes(b"changed after capture")
    canonicalize(snapshot)
    assert not snapshot.errors and snapshot.dataset_hash() == frozen_hash
    assert any(issue.code == "invalid_row_width" for issue in snapshot.issues)
