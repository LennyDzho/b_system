"""Define the typed source contracts and provenance used by the import pipeline."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.finance.policy import FinancialFact

SCHEMAS: dict[str, tuple[str, ...]] = {
    "properties.csv": ("property_id", "property_name", "cluster", "owner_id", "base_currency"),
    "reservations.csv": (
        "reservation_id",
        "property_id",
        "check_in",
        "check_out",
        "status",
        "gross_booking_value",
        "currency",
        "ota_fee",
        "refund_amount",
        "channel",
        "updated_at",
    ),
    "expenses.csv": (
        "expense_id",
        "property_id",
        "expense_date",
        "category",
        "amount",
        "currency",
        "payment_status",
    ),
    "payments.csv": (
        "payment_id",
        "reservation_id",
        "property_id",
        "payment_date",
        "amount",
        "currency",
        "status",
    ),
    "commission_rules.csv": (
        "property_id",
        "effective_from",
        "effective_to",
        "management_fee_rate",
        "fee_basis",
        "note",
    ),
}
KEYS = {
    "properties.csv": "property_id",
    "reservations.csv": "reservation_id",
    "expenses.csv": "expense_id",
    "payments.csv": "payment_id",
}
DATE_FIELDS = {
    "check_in",
    "check_out",
    "expense_date",
    "payment_date",
    "effective_from",
    "effective_to",
}
MONEY_FIELDS = {"gross_booking_value", "ota_fee", "refund_amount", "amount"}
TYPED_FIELDS = tuple(sorted(DATE_FIELDS | MONEY_FIELDS | {"updated_at", "management_fee_rate"}))


def json_default(value: Any) -> str:
    """Normalize exact decimals for stable typed read-back comparisons."""
    if isinstance(value, Decimal):
        with localcontext() as context:
            context.prec = max(50, len(value.as_tuple().digits))
            return format(value.normalize(), "f")
    return str(value)


def json_text(value: Any) -> str:
    """Serialize source values deterministically, representing Decimal as strings."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=json_default
    )


def digest(value: bytes) -> str:
    """Return SHA-256 without treating content identity as business deduplication."""
    return hashlib.sha256(value).hexdigest()


@dataclass
class RawRow:
    """An unchanged decoded CSV record and its physical starting line number."""

    source: str
    row_number: int
    payload: dict[str, Any]
    original_text: str
    business_key: str
    payload_hash: str


@dataclass
class Issue:
    """Explain a source/canonical problem without claiming financial inclusion."""

    code: str
    severity: str
    source: str
    row_number: int
    business_key: str
    property_id: str | None
    related_rows: list[int]
    actual_values: dict[str, Any]
    action: str
    description: str
    recommendation: str
    impact: str = "canonical_snapshot"
    inclusion: str = "not_calculated"
    related_entities: list[dict[str, Any]] = field(default_factory=list)
    period_start: date | None = None
    period_end: date | None = None
    effects: dict[str, Any] = field(default_factory=dict)
    is_global: bool = False


@dataclass
class CanonicalRecord:
    """One business entity with explicit type/reference status and source lineage."""

    source: str
    business_key: str
    property_id: str | None
    source_rows: list[int]
    payload_hash: str
    payload: dict[str, Any]
    typed: dict[str, date | datetime | Decimal | None]
    type_status: str
    reference_status: str = "not_checked"


@dataclass
class Snapshot:
    """Hold captured bytes, metadata, canonical entities, and base diagnostics."""

    files: dict[str, bytes] = field(default_factory=dict)
    manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: list[RawRow] = field(default_factory=list)
    canonical: list[CanonicalRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    financial_facts: list["FinancialFact"] = field(default_factory=list)

    def dataset_hash(self) -> str | None:
        """Hash an ordered complete byte manifest, independently of processing version."""
        if len(self.files) != len(SCHEMAS):
            return None
        return digest(
            json_text(
                {
                    name: {"sha256": digest(self.files[name]), "size": len(self.files[name])}
                    for name in sorted(self.files)
                }
            ).encode()
        )

    def counts(self) -> dict[str, int]:
        """Count source/canonical records without computing any monetary totals."""
        return {
            "raw": len(self.raw),
            "issues": len(self.issues),
            **{name: sum(row.source == name for row in self.canonical) for name in SCHEMAS},
        }
