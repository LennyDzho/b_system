"""Protect the agreed IDR methodology with pure functions and independent CSV fixtures."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.core.settings import BASE_DIR
from src.finance.policy import aggregate, calculate, management_fee
from src.services.imports.parser import canonicalize, capture
from tests.import_helpers import write_sources

EXPECTED = [
    (
        None,
        None,
        [1338916369, 86163513, 1252752856, 195531940, 222630407, 49462041, 785128468, 511162604],
        146,
        12,
    ),
    (
        date(2026, 9, 1),
        date(2026, 9, 30),
        [679364021, 40578408, 638785613, 101170044, 112849957, 22546329, 402219283, 263553633],
        77,
        6,
    ),
    (
        date(2026, 10, 1),
        date(2026, 10, 31),
        [636261198, 45585105, 590676093, 91233349, 105820954, 26915712, 366706078, 221842916],
        67,
        6,
    ),
    (
        date(2026, 11, 1),
        date(2026, 11, 30),
        [23291150, 0, 23291150, 3128547, 3959496, 0, 16203107, 25766055],
        2,
        0,
    ),
]
METRICS = ["gross", "refund", "net", "ota", "management", "expenses", "owner_estimate", "cash"]


def fixture_rows(directory: Path) -> dict[str, list[dict[str, str]]]:
    """Build mutable small source rows independently of the supplied business dataset."""
    write_sources(directory)
    snapshot = capture(directory)
    return {
        source: [deepcopy(r.payload) for r in snapshot.raw if r.source == source]
        for source in snapshot.files
    }


@pytest.mark.parametrize("start,end,expected,bookings,unknown", EXPECTED)
def test_supplied_dataset_all_and_monthly_controls(
    start: date | None, end: date | None, expected: list[int], bookings: int, unknown: int
) -> None:
    """Derive every section-9 total by generic rules; controls exist only in tests."""
    snapshot = canonicalize(capture(BASE_DIR / "data"))
    facts = calculate(snapshot)
    selected = [
        f
        for f in facts
        if (start is None or f.fact_date >= start) and (end is None or f.fact_date <= end)
    ]
    result = aggregate(selected)
    assert [Decimal(result["metrics"][name]["known_subtotal"]) for name in METRICS] == [
        Decimal(n) for n in expected
    ]
    assert result["coverage"]["reservations.csv"]["included"] == bookings
    assert result["unknown_fees"] == unknown
    for name in METRICS:
        allocated = sum(
            (
                Decimal(
                    aggregate([f for f in selected if f.property_id == prop])["metrics"][name][
                        "known_subtotal"
                    ]
                )
                for prop in {f.property_id for f in selected if f.property_id}
            ),
            Decimal(0),
        )
        assert allocated == Decimal(result["metrics"][name]["known_subtotal"])
    if start is None:
        assert result["coverage"]["expenses.csv"]["included"] == 89
        assert result["coverage"]["payments.csv"]["included"] == 74
        assert result["positive_payments"] == 67
        assert result["unreconciled_cash"] == "3998667.00"
        r2 = next(f for f in facts if f.business_key == "R0002")
        assert r2.management == 2002396
        assert r2.gross - r2.ota - r2.management == 8009582
        assert (
            sum(f.ota for f in facts if f.fee_reason == "CANCELLED_FEE_POLICY_UNKNOWN") == 12423868
        )


def test_commission_basis_rounding_and_input_guards() -> None:
    """Use gross rather than after-OTA basis and ROUND_HALF_UP per booking."""
    assert management_fee(Decimal(1000), Decimal("0.17")) == 170
    assert management_fee(Decimal(11778798), Decimal("0.17")) == 2002396
    assert management_fee(Decimal(5), Decimal("0.1")) == 1
    assert sum(management_fee(Decimal(5), Decimal("0.1")) for _ in range(2)) == 2
    for gross, rate in [("NaN", ".17"), ("100", "Infinity"), ("-1", ".17"), ("100", "1.01")]:
        with pytest.raises(ValueError):
            management_fee(Decimal(gross), Decimal(rate))


def test_cancelled_unknown_fee_and_zero_distinction(tmp_path: Path) -> None:
    """Full cancellation retains gross/refund/OTA; unknown management is not zero."""
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0].update(status="cancelled", refund_amount="1000")
    write_sources(tmp_path, rows)
    snapshot = canonicalize(capture(tmp_path))
    booking = next(f for f in calculate(snapshot) if f.source == "reservations.csv")
    result = aggregate([booking])
    assert booking.included and booking.management is None
    assert result["metrics"]["net"]["value"] == "0.00"
    assert result["metrics"]["owner_estimate"]["value"] == "-150.00"
    assert result["metrics"]["management"]["value"] is None
    assert result["metrics"]["management"]["known_subtotal"] == "0"
    assert aggregate([])["metrics"]["management"]["value"] == "0"


@pytest.mark.parametrize(
    "changes",
    [
        {"refund_amount": "1001"},
        {"ota_fee": "-1"},
        {"ota_fee": "1001"},
        {"gross_booking_value": "-1"},
        {"currency": "USD"},
        {"status": "unknown"},
        {"status": "cancelled", "refund_amount": "500"},
        {"check_out": "2026-08-01"},
    ],
)
def test_bad_accrual_never_erases_independent_cash(tmp_path: Path, changes: dict[str, str]) -> None:
    """Reject value-based anomalies without fallback, clamping or linked cash deletion."""
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0].update(changes)
    write_sources(tmp_path, rows)
    facts = calculate(canonicalize(capture(tmp_path)))
    assert not next(f for f in facts if f.source == "reservations.csv").included
    payments = [f for f in facts if f.source == "payments.csv"]
    assert all(f.included and f.reconciliation == "unreconciled" for f in payments)
    assert Decimal(aggregate(facts)["metrics"]["cash"]["value"]) == 200


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("missing", "MISSING_FEE_RULE"),
        ("overlap", "AMBIGUOUS_FEE_RULE"),
        ("conflict", "AMBIGUOUS_FEE_RULE"),
        ("basis", "UNSUPPORTED_FEE_BASIS"),
        ("rate", "INVALID_FEE_RULE"),
        ("bad_date", "AMBIGUOUS_FEE_RULE"),
    ],
)
def test_fee_rule_selection_is_never_arbitrary(tmp_path: Path, scenario: str, reason: str) -> None:
    """Unknown rules preserve known accrual and expose nullable fee plus reason."""
    rows = fixture_rows(tmp_path)
    rules = rows["commission_rules.csv"]
    if scenario == "missing":
        rules.clear()
    elif scenario in {"overlap", "conflict"}:
        extra = {**rules[0], "management_fee_rate": ".20"}
        if scenario == "overlap":
            extra["effective_from"] = "2026-09-01"
        rules.append(extra)
    else:
        rules[0].update(
            {
                "basis": {"fee_basis": "net"},
                "rate": {"management_fee_rate": "1.1"},
                "bad_date": {"effective_from": "bad"},
            }[scenario]
        )
    write_sources(tmp_path, rows)
    fact = next(
        f for f in calculate(canonicalize(capture(tmp_path))) if f.source == "reservations.csv"
    )
    assert fact.included and fact.management is None and fact.fee_reason == reason


def test_dates_pending_multiple_payments_and_rule_boundaries(tmp_path: Path) -> None:
    """Checkout accrual and payment-date cash differ; pending and multiple payments remain independent."""
    rows = fixture_rows(tmp_path)
    rows["reservations.csv"][0].update(
        check_in="2026-09-28", check_out="2026-10-03", refund_amount="100"
    )
    rows["commission_rules.csv"][0].update(effective_from="2026-09-28", effective_to="2026-09-28")
    rows["payments.csv"][0].update(amount="300", payment_date="2026-09-29")
    rows["payments.csv"][1].update(amount="450", payment_date="2026-09-30")
    write_sources(tmp_path, rows)
    facts = calculate(canonicalize(capture(tmp_path)))
    september = aggregate([f for f in facts if f.fact_date.month == 9])
    october = aggregate([f for f in facts if f.fact_date.month == 10])
    assert Decimal(september["metrics"]["gross"]["value"]) == 0
    assert Decimal(september["metrics"]["cash"]["value"]) == 750
    assert Decimal(september["metrics"]["expenses"]["value"]) == 20
    assert Decimal(october["metrics"]["gross"]["value"]) == 1000
    assert Decimal(october["metrics"]["management"]["value"]) == 170
    assert all(f.reconciliation == "reconciled" for f in facts if f.source == "payments.csv")


@pytest.mark.parametrize(
    "source,field,value",
    [
        ("payments.csv", "amount", "-1"),
        ("payments.csv", "currency", "USD"),
        ("payments.csv", "status", "pending"),
        ("payments.csv", "property_id", "foreign"),
        ("expenses.csv", "amount", "-1"),
        ("expenses.csv", "payment_status", "unknown"),
        ("expenses.csv", "currency", "USD"),
    ],
)
def test_independent_cash_and_expense_checks(
    tmp_path: Path, source: str, field: str, value: str
) -> None:
    """Invalid standalone money facts are excluded by their own values and current references."""
    rows = fixture_rows(tmp_path)
    rows[source][0][field] = value
    write_sources(tmp_path, rows)
    facts = calculate(canonicalize(capture(tmp_path)))
    key = rows[source][0]["payment_id" if source == "payments.csv" else "expense_id"]
    assert not next(f for f in facts if f.source == source and f.business_key == key).included


def test_cash_missing_booking_and_contradictory_property(tmp_path: Path) -> None:
    """Missing booking allows observed cash; a contradictory property never gets guessed."""
    rows = fixture_rows(tmp_path)
    rows["properties.csv"].append({**rows["properties.csv"][0], "property_id": "villa-2"})
    rows["payments.csv"][0]["reservation_id"] = "not-exported"
    rows["payments.csv"][1]["property_id"] = "villa-2"
    write_sources(tmp_path, rows)
    facts = calculate(canonicalize(capture(tmp_path)))
    first = next(f for f in facts if f.business_key == "payment-1")
    second = next(f for f in facts if f.business_key == "payment-2")
    assert first.included and first.reconciliation == "unreconciled"
    assert not second.included and "PROPERTY_MISMATCH" in second.reasons


@pytest.mark.parametrize(
    "source,key",
    [
        ("reservations.csv", "reservation_id"),
        ("payments.csv", "payment_id"),
        ("expenses.csv", "expense_id"),
    ],
)
def test_missing_business_id_is_an_explicit_financial_exclusion(
    tmp_path: Path, source: str, key: str
) -> None:
    """Money discarded before canonicalization must not become a complete zero silently."""
    rows = fixture_rows(tmp_path)
    rows[source][0][key] = ""
    write_sources(tmp_path, rows)
    facts = calculate(canonicalize(capture(tmp_path)))
    summary = aggregate(facts)
    assert summary["coverage"][source]["excluded"] == 1
    metric = {"reservations.csv": "gross", "payments.csv": "cash", "expenses.csv": "expenses"}[
        source
    ]
    assert not summary["metrics"][metric]["is_complete"]


def test_latest_bad_money_is_not_replaced_and_zero_rate_is_known(tmp_path: Path) -> None:
    """A newer invalid entity has no fallback; an explicit zero rule remains a known zero."""
    rows = fixture_rows(tmp_path)
    rows["commission_rules.csv"][0]["management_fee_rate"] = "0"
    write_sources(tmp_path, rows)
    valid = calculate(canonicalize(capture(tmp_path)))
    booking = next(f for f in valid if f.source == "reservations.csv")
    assert booking.management == 0 and aggregate([booking])["metrics"]["management"]["is_complete"]
    rows["reservations.csv"].append(
        {**rows["reservations.csv"][0], "updated_at": "2026-09-02", "ota_fee": "-1"}
    )
    write_sources(tmp_path, rows)
    facts = calculate(canonicalize(capture(tmp_path)))
    assert len([f for f in facts if f.source == "reservations.csv"]) == 1
    assert Decimal(aggregate(facts)["metrics"]["gross"]["value"]) == 0


def test_conflicting_old_rules_do_not_poison_current_rule(tmp_path: Path) -> None:
    """Only conflicts applicable at check-in make that booking's management unknown."""
    rows = fixture_rows(tmp_path)
    previous = {
        **rows["commission_rules.csv"][0],
        "effective_from": "2025-01-01",
        "effective_to": "2025-12-31",
    }
    rows["commission_rules.csv"].extend([previous, {**previous, "management_fee_rate": ".2"}])
    write_sources(tmp_path, rows)
    fact = next(
        f for f in calculate(canonicalize(capture(tmp_path))) if f.source == "reservations.csv"
    )
    assert fact.management == 170 and fact.fee_reason is None
