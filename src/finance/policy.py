"""Calculate source-based accrual and cash decisions without HTTP or database dependencies."""

import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from src.services.imports.records import CanonicalRecord, Issue, Snapshot

CALCULATION_VERSION = "idr-v1"
ZERO = Decimal("0")
MONEY_COLUMNS = ("gross", "refund", "ota", "management", "amount")
LABELS = {
    "INVALID_SOURCE": "Неоднозначная версия, неверный тип, статус или обязательное поле",
    "INVALID_ID": "Недопустимый формат ID",
    "UNKNOWN_PROPERTY": "Объект отсутствует в достоверном справочнике",
    "CURRENCY_MISMATCH": "Валюта операции и объекта не согласована с IDR; конвертация не выполнена",
    "INVALID_ARITHMETIC": "Отрицательная сумма, возврат больше gross или OTA больше gross",
    "PARTIAL_CANCELLATION_UNKNOWN": "Частичная отмена: право на оставшееся начисление не подтверждено",
    "CANCELLED_FEE_POLICY_UNKNOWN": "Management fee отмены неизвестна по условиям договора",
    "CANCELLED_OTA_RETAINED": "OTA отмены сохранена; политика возврата комиссии не подтверждена",
    "MISSING_FEE_RULE": "На дату заезда отсутствует правило комиссии",
    "AMBIGUOUS_FEE_RULE": "Нельзя однозначно определить одно действующее правило комиссии",
    "INVALID_FEE_RULE": "Некорректная ставка или параметры комиссионного правила",
    "UNSUPPORTED_FEE_BASIS": "База management fee не поддерживается",
    "PROPERTY_MISMATCH": "Объекты платежа и брони противоречат друг другу; cash не распределён",
    "UNRECONCILED_PAYMENT": "Cash сохранён, но не сверено соответствие начислениям брони",
}


@dataclass
class FinancialFact:
    """One canonical decision; excluded values stay observable and NULL stays unknown."""

    source: str
    business_key: str
    property_id: str | None
    fact_date: date | None
    currency: str
    included: bool
    source_rows: list[int]
    gross: Decimal | None = None
    refund: Decimal | None = None
    ota: Decimal | None = None
    management: Decimal | None = None
    amount: Decimal | None = None
    fee_reason: str | None = None
    reasons: list[str] = field(default_factory=list)
    reconciliation: str = "not_applicable"


def management_fee(gross: Decimal, rate: Decimal) -> Decimal:
    """Apply the gross basis in IDR, rounding each booking HALF_UP to one IDR.

    Inputs must be finite, nonnegative; rate is within [0,1]. Refund and OTA do
    not reduce this basis. The enlarged local context preserves source precision.
    """
    if not gross.is_finite() or not rate.is_finite() or gross < 0 or not ZERO <= rate <= 1:
        raise ValueError("Invalid commission inputs")
    with localcontext() as context:
        context.prec = max(50, len(gross.as_tuple().digits) + len(rate.as_tuple().digits) + 10)
        return (gross * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def fee_for(
    record: CanonicalRecord, rules: list[CanonicalRecord]
) -> tuple[Decimal | None, str | None]:
    """Select exactly one inclusive rule at check-in; cancellations retain unknown fees."""
    if record.payload["status"] == "cancelled":
        return None, "CANCELLED_FEE_POLICY_UNKNOWN"
    matching = []
    for rule in rules:
        if rule.property_id != record.property_id:
            continue
        start, end = rule.typed.get("effective_from"), rule.typed.get("effective_to")
        if rule.type_status == "conflict":
            # Conflicting rates share a business key containing their dates. A
            # conflict outside this stay must not poison an unrelated valid rule.
            try:
                bounds = {
                    (
                        date.fromisoformat(candidate["effective_from"]),
                        date.fromisoformat(candidate["effective_to"]),
                    )
                    for candidate in rule.payload["candidates"]
                }
                if len(bounds) == 1:
                    start, end = next(iter(bounds))
            except (ValueError, TypeError, KeyError):
                pass
        if start is None or end is None or end < start:
            return None, "AMBIGUOUS_FEE_RULE"
        if start <= record.typed["check_in"] <= end:
            if rule.type_status == "conflict":
                return None, "AMBIGUOUS_FEE_RULE"
            matching.append(rule)
    if not matching:
        return None, "MISSING_FEE_RULE"
    if len(matching) != 1:
        return None, "AMBIGUOUS_FEE_RULE"
    rule = matching[0]
    rate = rule.typed.get("management_fee_rate")
    if rule.type_status != "valid" or not isinstance(rate, Decimal) or not ZERO <= rate <= 1:
        return None, "INVALID_FEE_RULE"
    if rule.payload.get("fee_basis") != "gross_booking_value":
        return None, "UNSUPPORTED_FEE_BASIS"
    return management_fee(record.typed["gross_booking_value"], rate), None


def calculate(snapshot: Snapshot) -> list[FinancialFact]:
    """Append financial diagnostics and calculate independent IDR source decisions.

    Gross/refund/OTA accrue on checkout, expenses on expense_date (paid+pending),
    received payments on payment_date. Invalid bookings never erase a standalone
    valid cash receipt. No refund payment or payout is invented from accrual data.
    """
    properties = {
        r.business_key: r
        for r in snapshot.canonical
        if r.source == "properties.csv" and r.type_status == "valid"
    }
    bookings = {r.business_key: r for r in snapshot.canonical if r.source == "reservations.csv"}
    rules = [r for r in snapshot.canonical if r.source == "commission_rules.csv"]
    records = list(snapshot.canonical)
    # Keep raw rows without business IDs visible as exclusions in financial coverage.
    from src.services.imports.parser import typed_values

    for raw_row in snapshot.raw:
        if (
            raw_row.source in {"reservations.csv", "expenses.csv", "payments.csv"}
            and not raw_row.business_key.strip()
        ):
            key = f"!missing-key:{raw_row.row_number}"
            while any(r.source == raw_row.source and r.business_key == key for r in records):
                key = "!" + key
            typed, _ = typed_values(snapshot, raw_row)
            records.append(
                CanonicalRecord(
                    raw_row.source,
                    key,
                    raw_row.payload.get("property_id"),
                    [raw_row.row_number],
                    raw_row.payload_hash,
                    raw_row.payload,
                    typed,
                    "invalid",
                )
            )
    facts: list[FinancialFact] = []
    by_booking: dict[str, FinancialFact] = {}
    cash_groups: dict[str, list[FinancialFact]] = defaultdict(list)
    for record in records:
        if record.source not in {"reservations.csv", "expenses.csv", "payments.csv"}:
            continue
        dates = {
            "reservations.csv": "check_out",
            "expenses.csv": "expense_date",
            "payments.csv": "payment_date",
        }
        prop = properties.get(record.property_id)
        fact = FinancialFact(
            record.source,
            record.business_key,
            record.property_id if prop else None,
            record.typed.get(dates[record.source]),
            record.payload.get("currency") or "UNKNOWN",
            True,
            record.source_rows,
        )
        if record.type_status != "valid":
            fact.reasons.append("INVALID_SOURCE")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record.business_key) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", record.property_id or ""
        ):
            fact.reasons.append("INVALID_ID")
        if prop is None:
            fact.reasons.append("UNKNOWN_PROPERTY")
        if fact.currency != "IDR" or (prop and prop.payload["base_currency"] != fact.currency):
            fact.reasons.append("CURRENCY_MISMATCH")
        if record.source == "reservations.csv":
            fact.gross, fact.refund, fact.ota = (
                record.typed.get(n) for n in ("gross_booking_value", "refund_amount", "ota_fee")
            )
            if all(isinstance(v, Decimal) for v in (fact.gross, fact.refund, fact.ota)):
                if (
                    min(fact.gross, fact.refund, fact.ota) < 0
                    or fact.refund > fact.gross
                    or fact.ota > fact.gross
                ):
                    fact.reasons.append("INVALID_ARITHMETIC")
                if record.payload.get("status") == "cancelled" and fact.refund < fact.gross:
                    fact.reasons.append("PARTIAL_CANCELLATION_UNKNOWN")
            fact.included = not fact.reasons
            if fact.included:
                fact.management, fact.fee_reason = fee_for(record, rules)
                if fact.fee_reason:
                    fact.reasons.append(fact.fee_reason)
                if record.payload["status"] == "cancelled" and fact.ota > 0:
                    fact.reasons.append("CANCELLED_OTA_RETAINED")
            by_booking[record.business_key] = fact
        else:
            fact.amount = record.typed.get("amount")
            if isinstance(fact.amount, Decimal) and fact.amount < 0:
                fact.reasons.append("INVALID_ARITHMETIC")
            if record.source == "payments.csv":
                if not re.fullmatch(
                    r"[A-Za-z0-9_-]{1,64}", record.payload.get("reservation_id") or ""
                ):
                    fact.reasons.append("INVALID_ID")
                booking = bookings.get(record.payload.get("reservation_id"))
                # A conflicting booking cannot supply a reliable attribution.
                if (
                    booking
                    and booking.property_id is not None
                    and booking.property_id != record.property_id
                ):
                    fact.reasons.append("PROPERTY_MISMATCH")
                cash_groups[record.payload.get("reservation_id", "")].append(fact)
            fact.included = not fact.reasons
        facts.append(fact)
    for key, payments in cash_groups.items():
        booking = by_booking.get(key)
        total = sum((f.amount for f in payments if f.included), ZERO)
        source_booking = bookings.get(key)
        verified = bool(
            booking
            and booking.included
            and source_booking.payload.get("currency") == "IDR"
            and total == max(booking.gross - booking.refund - booking.ota, ZERO)
        )
        for fact in payments:
            fact.reconciliation = "reconciled" if fact.included and verified else "unreconciled"
            if fact.included and not verified:
                fact.reasons.append("UNRECONCILED_PAYMENT")
    raw = {(r.source, r.row_number): r for r in snapshot.raw}
    for fact in facts:
        record = next(
            r for r in records if r.source == fact.source and r.business_key == fact.business_key
        )
        selected = next(
            (
                raw[fact.source, n]
                for n in fact.source_rows
                if raw[fact.source, n].payload_hash == record.payload_hash
            ),
            raw[fact.source, fact.source_rows[0]],
        )
        for code in fact.reasons:
            scope = (
                "accrual"
                if fact.source == "reservations.csv"
                else "cash"
                if fact.source == "payments.csv"
                else "expenses"
            )
            action = (
                "unknown_fee"
                if code == fact.fee_reason
                else "include_with_warning"
                if fact.included
                else "exclude_" + scope
            )
            snapshot.issues.append(
                Issue(
                    code,
                    "warning" if fact.included else "error",
                    fact.source,
                    selected.row_number,
                    fact.business_key,
                    fact.property_id,
                    fact.source_rows,
                    selected.payload,
                    action,
                    LABELS[code],
                    "Уточнить или исправить данные/условия в источнике",
                    "management" if code == fact.fee_reason else scope,
                    "included" if fact.included else "excluded",
                )
            )
    return facts


def aggregate(facts: list[FinancialFact]) -> dict[str, Any]:
    """Sum persisted independent facts with Decimal, retaining per-domain coverage.

    Empty known selections have real zeros. Unknown fees expose NULL total plus
    known subtotal; the owner figure always remains an estimate before unresolved
    fees, never a cash balance, receivable, or amount payable to an owner.
    """
    reservations = [f for f in facts if f.source == "reservations.csv" and f.included]
    expenses = [f for f in facts if f.source == "expenses.csv" and f.included]
    payments = [f for f in facts if f.source == "payments.csv" and f.included]
    unknown = [f for f in reservations if f.management is None]
    values = {
        name: sum((getattr(f, name) for f in reservations if getattr(f, name) is not None), ZERO)
        for name in ("gross", "refund", "ota", "management")
    }
    values.update(
        expenses=sum((f.amount for f in expenses), ZERO),
        cash=sum((f.amount for f in payments), ZERO),
    )
    values["net"] = values["gross"] - values["refund"]
    values["after_ota"] = values["net"] - values["ota"]
    values["owner_estimate"] = values["after_ota"] - values["management"] - values["expenses"]
    values["accrual_cash_difference"] = values["after_ota"] - values["cash"]
    coverage = {}
    for source in ("reservations.csv", "expenses.csv", "payments.csv"):
        group = [f for f in facts if f.source == source]
        coverage[source] = {
            "included": sum(f.included for f in group),
            "excluded": sum(not f.included for f in group),
            "undated": sum(f.fact_date is None for f in group),
        }
    domains = {
        "expenses": ("expenses.csv",),
        "cash": ("payments.csv",),
        "owner_estimate": ("reservations.csv", "expenses.csv"),
        "accrual_cash_difference": ("reservations.csv", "payments.csv"),
    }
    metrics = {}
    for name, subtotal in values.items():
        sources = domains.get(name, ("reservations.csv",))
        incomplete = any(coverage[s]["excluded"] for s in sources)
        fee_unknown = len(unknown) if name in {"management", "owner_estimate"} else 0
        metrics[name] = {
            "value": None if name == "management" and unknown else format(subtotal, "f"),
            "known_subtotal": format(subtotal, "f"),
            "unknown_count": fee_unknown,
            "is_complete": not incomplete and not fee_unknown,
            "is_estimate": name == "owner_estimate",
            "reasons": dict(Counter(f.fee_reason for f in unknown)) if fee_unknown else {},
        }
    return {
        "metrics": metrics,
        "coverage": coverage,
        "unknown_fees": len(unknown),
        "unreconciled_cash": format(
            sum((f.amount for f in payments if f.reconciliation == "unreconciled"), ZERO), "f"
        ),
        "unreconciled_count": sum(f.reconciliation == "unreconciled" for f in payments),
        "positive_payments": sum(f.amount > 0 for f in payments),
        "reasons": dict(Counter(reason for f in facts for reason in f.reasons)),
    }


def fact_dict(fact: FinancialFact) -> dict[str, Any]:
    """Expose exact source units as decimal strings and dates as ISO text."""
    data = asdict(fact)
    for column in MONEY_COLUMNS:
        data[column] = format(data[column], "f") if data[column] is not None else None
    data["fact_date"] = fact.fact_date.isoformat() if fact.fact_date else None
    return data
