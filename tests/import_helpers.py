"""Build small independent five-file fixtures without changing user CSV sources."""

import csv
from pathlib import Path

from src.services.imports.records import SCHEMAS


def write_sources(
    directory: Path, overrides: dict[str, list[dict[str, str]]] | None = None
) -> None:
    """Write BOM-encoded CSVs with two distinct payments for one booking."""
    rows = {
        "properties.csv": [
            {
                "property_id": "villa-1",
                "property_name": "Villa",
                "cluster": "Area",
                "owner_id": "owner-1",
                "base_currency": "IDR",
            }
        ],
        "reservations.csv": [
            {
                "reservation_id": "booking-1",
                "property_id": "villa-1",
                "check_in": "2026-09-01",
                "check_out": "2026-09-03",
                "status": "confirmed",
                "gross_booking_value": "1000",
                "currency": "IDR",
                "ota_fee": "150",
                "refund_amount": "0",
                "channel": "OTA",
                "updated_at": "2026-09-01",
            }
        ],
        "expenses.csv": [
            {
                "expense_id": "expense-1",
                "property_id": "villa-1",
                "expense_date": "2026-09-02",
                "category": "service",
                "amount": "20",
                "currency": "IDR",
                "payment_status": "pending",
            }
        ],
        "payments.csv": [
            {
                "payment_id": key,
                "reservation_id": "booking-1",
                "property_id": "villa-1",
                "payment_date": "2026-09-03",
                "amount": "100",
                "currency": "IDR",
                "status": "received",
            }
            for key in ("payment-1", "payment-2")
        ],
        "commission_rules.csv": [
            {
                "property_id": "villa-1",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "management_fee_rate": "0.17",
                "fee_basis": "gross_booking_value",
                "note": "fixture",
            }
        ],
    }
    rows.update(overrides or {})
    directory.mkdir(parents=True, exist_ok=True)
    for name, values in rows.items():
        with (directory / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=SCHEMAS[name])
            writer.writeheader()
            writer.writerows(values)
