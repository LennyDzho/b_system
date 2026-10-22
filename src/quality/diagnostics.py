"""Add deterministic occupancy, channel, payment and rule diagnostics to calculated snapshots."""

from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any

from src.finance.policy import aggregate, fact_dict
from src.services.imports.parser import parse_date
from src.services.imports.records import CanonicalRecord, Issue, Snapshot


def diagnose(snapshot: Snapshot) -> None:
    """Enrich existing decisions and append warnings; never mutate monetary facts.

    Occupancy uses canonical active stays before financial exclusions, with
    half-open stay dates. Rule endpoints are inclusive. Payment comparisons use
    the existing Decimal aggregation of independently admitted financial facts.
    """
    records = {(r.source, r.business_key): r for r in snapshot.canonical}
    raw = {(r.source, r.row_number): r for r in snapshot.raw}
    known = {
        r.business_key
        for r in snapshot.canonical
        if r.source == "properties.csv" and r.type_status == "valid"
    }
    facts = {(f.source, f.business_key): f for f in snapshot.financial_facts}

    def reference(record: CanonicalRecord) -> dict[str, Any]:
        """Retain cross-source identity and all raw lineage for permission-aware rendering."""
        return {
            "source": record.source,
            "business_key": record.business_key,
            "property_id": record.property_id if record.property_id in known else None,
            "source_rows": record.source_rows,
        }

    def emit(
        record: CanonicalRecord,
        code: str,
        description: str,
        impact: str,
        related: list[CanonicalRecord] | None = None,
        values: dict[str, Any] | None = None,
        severity: str = "warning",
        global_issue: bool = False,
    ) -> Issue:
        """Create one issue whose financial effects are supplied by existing facts."""
        selected = next(
            (
                raw[record.source, n]
                for n in record.source_rows
                if raw[record.source, n].payload_hash == record.payload_hash
            ),
            raw[record.source, record.source_rows[0]],
        )
        issue = Issue(
            code,
            severity,
            record.source,
            selected.row_number,
            record.business_key,
            record.property_id if record.property_id in known else None,
            record.source_rows,
            values if values is not None else selected.payload,
            "preserve_financial_decision",
            description,
            "Проверить исходные записи и условия договора; предупреждение само по себе не меняет допуск сумм",
            impact,
            "see_financial_decisions",
            related_entities=[reference(r) for r in related or []],
            is_global=global_issue,
        )
        snapshot.issues.append(issue)
        return issue

    stays = [
        r
        for r in snapshot.canonical
        if r.source == "reservations.csv"
        and r.property_id in known
        and r.payload.get("status") in {"confirmed", "modified"}
        and isinstance(r.typed.get("check_in"), date)
        and isinstance(r.typed.get("check_out"), date)
        and r.typed["check_in"] < r.typed["check_out"]
    ]
    for left, right in combinations(stays, 2):
        if left.property_id != right.property_id:
            continue
        start = max(left.typed["check_in"], right.typed["check_in"])
        end = min(left.typed["check_out"], right.typed["check_out"])
        if start < end:
            issue = emit(
                left,
                "OCCUPANCY_OVERLAP",
                f"Пересекаются проживания {left.business_key} и {right.business_key}; это не доказанный дубль",
                "occupancy",
                [right],
                {
                    "left": left.payload,
                    "right": right.payload,
                    "overlap_from": start.isoformat(),
                    "overlap_to_exclusive": end.isoformat(),
                },
            )
            issue.period_start, issue.period_end = start, end - timedelta(days=1)
    for record in snapshot.canonical:
        ota = record.typed.get("ota_fee")
        if (
            record.source == "reservations.csv"
            and record.payload.get("channel") == "Direct"
            and isinstance(ota, Decimal)
            and ota > 0
        ):
            emit(
                record,
                "DIRECT_WITH_OTA",
                "У Direct указана положительная OTA: исходная комиссия сохранена",
                "accrual",
            )

    payments: dict[str, list[CanonicalRecord]] = defaultdict(list)
    for record in snapshot.canonical:
        if record.source != "payments.csv":
            continue
        key = record.payload.get("reservation_id")
        payments[key].append(record)
        booking = records.get(("reservations.csv", key))
        amount = record.typed.get("amount")
        if amount == 0 and record.payload.get("status") == "received":
            emit(
                record,
                "ZERO_RECEIVED_PAYMENT",
                "Нулевой received — нулевое движение; это не подтверждение денежного возврата",
                "cash",
                [booking] if booking else [],
                severity="info",
            )
        if booking and record.payload.get("currency") != booking.payload.get("currency"):
            emit(
                record,
                "PAYMENT_BOOKING_CURRENCY",
                "Валюты платежа и связанной брони различаются; самостоятельное решение по cash сохранено",
                "cash_reconciliation",
                [booking],
            )
    for record in snapshot.canonical:
        if record.source != "reservations.csv":
            continue
        linked = payments.get(record.business_key, [])
        if not linked:
            emit(
                record,
                "PAYMENT_NOT_EXPORTED",
                "В выгрузке нет платежа по брони; это не доказанная просрочка",
                "cash_reconciliation",
                severity="info",
            )
            continue
        booking_fact = facts.get((record.source, record.business_key))
        cash = [
            facts[p.source, p.business_key] for p in linked if (p.source, p.business_key) in facts
        ]
        if booking_fact and booking_fact.included and all(f.included for f in cash):
            expected = max(
                Decimal(aggregate([booking_fact])["metrics"]["after_ota"]["known_subtotal"]),
                Decimal(0),
            )
            received = Decimal(aggregate(cash)["metrics"]["cash"]["known_subtotal"])
            if received != expected:
                emit(
                    record,
                    "PAYMENT_AMOUNT_DIFFERENCE",
                    "Зарегистрированный cash отличается от сопоставимых начислений после OTA; разница не является точной дебиторской задолженностью",
                    "cash_reconciliation",
                    linked,
                    {
                        "booking": record.payload,
                        "expected_after_ota": str(expected),
                        "registered_cash": str(received),
                        "difference": str(expected - received),
                        "currency": "IDR",
                    },
                )

    rules: dict[str, list[CanonicalRecord]] = defaultdict(list)
    for rule in snapshot.canonical:
        if rule.source != "commission_rules.csv":
            continue
        rules[rule.property_id].append(rule)
        rate = rule.typed.get("management_fee_rate")
        if (
            rule.type_status != "valid"
            or not isinstance(rate, Decimal)
            or not Decimal(0) <= rate <= 1
            or rule.payload.get("fee_basis") != "gross_booking_value"
        ):
            emit(
                rule,
                "INVALID_COMMISSION_RULE",
                "Комиссионное правило некорректно, конфликтует или использует неподдерживаемую базу",
                "management",
                global_issue=True,
            )
    for group in rules.values():
        intervals = [
            r
            for r in group
            if isinstance(r.typed.get("effective_from"), date)
            and isinstance(r.typed.get("effective_to"), date)
            and r.typed["effective_from"] <= r.typed["effective_to"]
        ]
        for left, right in combinations(intervals, 2):
            if max(left.typed["effective_from"], right.typed["effective_from"]) <= min(
                left.typed["effective_to"], right.typed["effective_to"]
            ):
                emit(
                    left,
                    "COMMISSION_RULE_OVERLAP",
                    "Включительные интервалы комиссионных правил пересекаются",
                    "management",
                    [right],
                    global_issue=True,
                )
        ordered = sorted(intervals, key=lambda r: r.typed["effective_from"])
        if ordered:
            end, previous = ordered[0].typed["effective_to"], ordered[0]
            for current in ordered[1:]:
                if current.typed["effective_from"] > end + timedelta(days=1):
                    emit(
                        current,
                        "COMMISSION_RULE_GAP",
                        "Между правилами есть непокрытый интервал; внешний календарь не считается доказанным пропуском",
                        "management",
                        [previous],
                        {
                            "gap_from": (end + timedelta(days=1)).isoformat(),
                            "gap_to": (
                                current.typed["effective_from"] - timedelta(days=1)
                            ).isoformat(),
                        },
                        global_issue=True,
                    )
                if current.typed["effective_to"] > end:
                    end, previous = current.typed["effective_to"], current

    # Enrich both base and financial diagnostics with explicit per-domain decisions.
    for issue in snapshot.issues:
        record = records.get((issue.source, issue.business_key))
        if not issue.related_entities:
            if issue.source == "payments.csv":
                row = raw.get((issue.source, issue.row_number))
                booking = (
                    records.get(("reservations.csv", row.payload.get("reservation_id")))
                    if row
                    else None
                )
                if booking:
                    issue.related_entities.append(reference(booking))
            elif issue.related_rows:
                issue.related_entities.append(
                    {
                        "source": issue.source,
                        "business_key": issue.business_key,
                        "property_id": issue.property_id,
                        "source_rows": issue.related_rows,
                    }
                )
        primary = facts.get((issue.source, issue.business_key))
        if primary is None:
            primary = next(
                (
                    f
                    for f in snapshot.financial_facts
                    if f.source == issue.source and issue.row_number in f.source_rows
                ),
                None,
            )
        related_facts = [
            facts.get((r["source"], r["business_key"])) for r in issue.related_entities
        ]
        issue.effects = {
            "facts": [fact_dict(f) for f in [primary, *related_facts] if f is not None],
            "note": "Решения по начислениям, cash и комиссии независимы; дополнительные предупреждения не меняют суммы",
        }
        if issue.period_start is None and issue.source in {
            "reservations.csv",
            "payments.csv",
            "expenses.csv",
        }:
            row = raw.get((issue.source, issue.row_number))
            field = {
                "reservations.csv": "check_out",
                "payments.csv": "payment_date",
                "expenses.csv": "expense_date",
            }[issue.source]
            try:
                issue.period_start = issue.period_end = (
                    parse_date(row.payload[field]) if row else None
                )
            except (ValueError, TypeError, KeyError):
                issue.period_start = issue.period_end = None
        issue.is_global = (
            issue.is_global
            or issue.source in {"properties.csv", "commission_rules.csv"}
            or issue.property_id is None
        )
        if issue.inclusion == "not_calculated":
            original_row = raw.get((issue.source, issue.row_number))
            if issue.action in {"exclude_duplicate", "exclude_superseded"} or (
                record is not None
                and original_row is not None
                and original_row.payload_hash != record.payload_hash
            ):
                issue.inclusion = "excluded_raw"
            elif primary:
                issue.inclusion = "included" if primary.included else "excluded"


def issue_counts(issues: list[Issue]) -> dict[str, Any]:
    """Count issues, physical rows and business entities including both sides of a pair."""
    rows: set[tuple[str, int]] = set()
    entities: set[tuple[str, str, str]] = set()
    for issue in issues:
        rows.add((issue.source, issue.row_number))
        primary = next(
            (
                f
                for f in issue.effects.get("facts", [])
                if f["source"] == issue.source and issue.row_number in f["source_rows"]
            ),
            None,
        )
        if primary:
            entities.add((issue.source, "business", primary["business_key"]))
        elif issue.business_key.strip():
            entities.add((issue.source, "business", issue.business_key))
        else:
            entities.add((issue.source, "raw", str(issue.row_number)))
        rows.update((issue.source, n) for n in issue.related_rows)
        for related in issue.related_entities:
            rows.update((related["source"], n) for n in related["source_rows"])
            entities.add((related["source"], "business", related["business_key"]))
    return {
        "issues": len(issues),
        "affected_rows": len(rows),
        "affected_entities": len(entities),
        "by_code": dict(sorted(Counter(i.code for i in issues).items())),
        "snapshot_limitations": sum(
            i.is_global or i.period_start is None or i.period_end is None for i in issues
        ),
        "excluded_financial_entities": len(
            {
                (f["source"], f["business_key"])
                for issue in issues
                for f in issue.effects.get("facts", [])
                if not f["included"]
            }
        ),
        "discarded_raw_rows": len(
            {
                (issue.source, issue.row_number)
                for issue in issues
                if issue.action in {"exclude_duplicate", "exclude_superseded"}
            }
        ),
    }
