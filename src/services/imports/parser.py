"""Capture CSV bytes and apply structural canonicalization without money formulas."""

import csv
import io
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, DecimalException
from pathlib import Path

from src.services.imports.records import (
    DATE_FIELDS,
    KEYS,
    MONEY_FIELDS,
    SCHEMAS,
    CanonicalRecord,
    Issue,
    RawRow,
    Snapshot,
    digest,
    json_text,
)


def capture(directory: Path) -> Snapshot:
    """Read all five inputs once, retaining available metadata even on preflight failure."""
    snapshot = Snapshot()
    for name in sorted(SCHEMAS):
        try:
            data = (directory / name).read_bytes()
        except OSError as error:
            snapshot.errors.append(f"{name}: {type(error).__name__}")
            snapshot.manifest[name] = {"error": type(error).__name__}
            continue
        snapshot.files[name] = data
        snapshot.manifest[name] = {"sha256": digest(data), "size": len(data), "rows": None}
    for name, data in snapshot.files.items():
        try:
            content = data.decode("utf-8-sig")
            lines = list(io.StringIO(content, newline=""))
            reader = csv.reader(io.StringIO(content, newline=""), strict=True)
            header = next(reader, [])
            if len(header) != len(set(header)) or set(header) != set(SCHEMAS[name]):
                raise ValueError("invalid_header")
            count = 0
            while True:
                start = reader.line_num + 1
                try:
                    values = next(reader)
                except StopIteration:
                    break
                payload = {
                    column: values[index] if index < len(values) else None
                    for index, column in enumerate(header)
                }
                if len(values) != len(header):
                    payload["__row_values"] = values
                if name == "commission_rules.csv":
                    key = (
                        json_text(
                            [
                                payload.get(column)
                                for column in ("property_id", "effective_from", "effective_to")
                            ]
                        )
                        if payload.get("property_id")
                        else ""
                    )
                else:
                    key = str(payload.get(KEYS[name]) or "")
                snapshot.raw.append(
                    RawRow(
                        name,
                        start,
                        payload,
                        "".join(lines[start - 1 : reader.line_num]),
                        key,
                        digest(json_text(payload).encode()),
                    )
                )
                count += 1
            snapshot.manifest[name]["rows"] = count
        except (UnicodeError, csv.Error, ValueError) as error:
            snapshot.errors.append(f"{name}: unusable CSV/schema ({type(error).__name__})")
    return snapshot


def add_issue(
    snapshot: Snapshot,
    row: RawRow,
    code: str,
    description: str,
    action: str = "review",
    related: list[int] | None = None,
) -> None:
    """Attach actual input values and lineage without implying calculated inclusion."""
    snapshot.issues.append(
        Issue(
            code,
            "warning" if code in ("duplicate", "superseded") else "error",
            row.source,
            row.row_number,
            row.business_key,
            row.payload.get("property_id"),
            related or [],
            row.payload,
            action,
            description,
            "Уточнить запись в источнике; не исправлять исходный CSV.",
        )
    )


def parse_timestamp(value: object) -> datetime:
    """Interpret ISO dates/naive timestamps as UTC; normalize aware versions to UTC."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T| )", value):
        raise ValueError("invalid_timestamp")
    parsed = datetime.fromisoformat(value)
    parsed = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    if not 1900 <= parsed.year <= 2299:
        raise ValueError("timestamp_out_of_storage_range")
    return parsed


def parse_date(value: object) -> date:
    """Parse the shared strict source date contract within the accepted Date32 range."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_date")
    parsed = date.fromisoformat(value)
    if not 1900 <= parsed.year <= 2299:
        raise ValueError("date_out_of_storage_range")
    return parsed


def parse_decimal(value: object, scale: int, precision: int) -> Decimal:
    """Preserve exact source decimals; reject nonfinite, overflow, and lossy scale."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing_decimal")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed.copy_abs() >= Decimal(10) ** (precision - scale):
        raise ValueError("decimal_out_of_storage_range")
    quantized = parsed.quantize(Decimal(1).scaleb(-scale))
    if parsed != quantized:
        raise ValueError("decimal_scale_loss")
    return quantized


def typed_values(
    snapshot: Snapshot, row: RawRow
) -> tuple[dict[str, date | datetime | Decimal | None], bool]:
    """Parse source types into nullable CH columns, preserving every invalid raw value."""
    typed: dict[str, date | datetime | Decimal | None] = {}
    valid = "__row_values" not in row.payload
    if not valid:
        add_issue(snapshot, row, "invalid_row_width", "Число полей не соответствует заголовку")
    for name, value in row.payload.items():
        if name not in DATE_FIELDS | MONEY_FIELDS | {"updated_at", "management_fee_rate"}:
            if (
                name != "note"
                and name in SCHEMAS[row.source]
                and (not isinstance(value, str) or not value.strip())
            ):
                valid = False
                add_issue(snapshot, row, "missing_value", f"Отсутствует обязательное поле {name}")
            continue
        try:
            if name in DATE_FIELDS:
                typed[name] = parse_date(value)
            elif name == "updated_at":
                typed[name] = parse_timestamp(value)
            else:
                typed[name] = parse_decimal(
                    value,
                    8 if name == "management_fee_rate" else 2,
                    18 if name == "management_fee_rate" else 20,
                )
        except (ValueError, DecimalException, OverflowError):
            typed[name] = None
            valid = False
            add_issue(
                snapshot, row, "invalid_type", f"Поле {name} не представимо в объявленном типе"
            )
    allowed = {
        "reservations.csv": ("status", {"confirmed", "modified", "cancelled"}),
        "payments.csv": ("status", {"received"}),
        "expenses.csv": ("payment_status", {"paid", "pending"}),
    }
    if row.source in allowed:
        column, choices = allowed[row.source]
        if row.payload.get(column) not in choices:
            valid = False
            add_issue(
                snapshot, row, "unknown_status", "Неизвестный статус не заменён подтверждённым"
            )
    for start, end in (("check_in", "check_out"), ("effective_from", "effective_to")):
        if typed.get(start) is not None and typed.get(end) is not None:
            if typed[end] < typed[start] or (start == "check_in" and typed[end] == typed[start]):
                valid = False
                add_issue(snapshot, row, "invalid_date_range", "Некорректный интервал дат")
    return typed, valid


def canonicalize(snapshot: Snapshot) -> Snapshot:
    """Select business versions without row-order winners or financial eligibility claims."""
    groups: dict[tuple[str, str], list[RawRow]] = defaultdict(list)
    for row in snapshot.raw:
        if not row.business_key.strip():
            add_issue(snapshot, row, "missing_key", "Отсутствует бизнес-ключ", "exclude_canonical")
        else:
            groups[row.source, row.business_key].append(row)
    for (source, key), rows in sorted(groups.items()):
        by_hash: dict[str, list[RawRow]] = defaultdict(list)
        for row in rows:
            by_hash[row.payload_hash].append(row)
        unique = [group[0] for _, group in sorted(by_hash.items())]
        parsed = {row.payload_hash: typed_values(snapshot, row) for row in unique}
        for group in by_hash.values():
            for duplicate in group[1:]:
                add_issue(
                    snapshot,
                    duplicate,
                    "duplicate",
                    "Полный дубль сохранён в raw, каноническая запись одна",
                    "exclude_duplicate",
                    [group[0].row_number],
                )
        selected: RawRow | None = unique[0]
        if source == "reservations.csv":
            try:
                versions = [(parse_timestamp(row.payload.get("updated_at")), row) for row in unique]
                latest = max(version for version, _ in versions)
                winners = [row for version, row in versions if version == latest]
                selected = winners[0] if len(winners) == 1 else None
            except (ValueError, OverflowError):
                selected = None
        elif len(unique) != 1:
            selected = None
        if selected is None:
            for row in unique:
                add_issue(
                    snapshot,
                    row,
                    "version_conflict",
                    "Нельзя однозначно выбрать актуальную запись",
                    "exclude_canonical_values",
                    [other.row_number for other in rows if other is not row],
                )
            properties = {row.payload.get("property_id") for row in rows}
            payload = {"candidates": [row.payload for row in unique]}
            record = CanonicalRecord(
                source,
                key,
                next(iter(properties)) if len(properties) == 1 else None,
                sorted(row.row_number for row in rows),
                digest(json_text(payload).encode()),
                payload,
                {},
                "conflict",
            )
        else:
            typed, valid = parsed[selected.payload_hash]
            for row in unique:
                if row is not selected:
                    add_issue(
                        snapshot,
                        row,
                        "superseded",
                        "Более ранняя версия сохранена в raw",
                        "exclude_superseded",
                        [selected.row_number],
                    )
            record = CanonicalRecord(
                source,
                key,
                selected.payload.get("property_id"),
                sorted(row.row_number for row in rows),
                selected.payload_hash,
                selected.payload,
                typed,
                "valid" if valid else "invalid",
            )
        snapshot.canonical.append(record)
    validate_references(snapshot)
    return snapshot


def validate_references(snapshot: Snapshot) -> None:
    """Evaluate reference identity separately from future financial admission decisions."""
    properties = {
        row.business_key: row for row in snapshot.canonical if row.source == "properties.csv"
    }
    reservations = {
        row.business_key: row for row in snapshot.canonical if row.source == "reservations.csv"
    }
    raw_by_number = {(row.source, row.row_number): row for row in snapshot.raw}
    for record in snapshot.canonical:
        if record.type_status == "conflict":
            record.reference_status = "ambiguous"
            continue
        record.reference_status = "known"
        raw = next(
            raw_by_number[record.source, number]
            for number in record.source_rows
            if raw_by_number[record.source, number].payload_hash == record.payload_hash
        )
        prop = properties.get(record.property_id or "")
        if prop is None or prop.type_status != "valid":
            record.reference_status = "unresolved"
            add_issue(
                snapshot, raw, "unknown_property", "Ссылка на объект отсутствует или неоднозначна"
            )
            snapshot.issues[-1].property_id = None
        if record.source == "payments.csv" and record.type_status != "conflict":
            reservation = reservations.get(record.payload.get("reservation_id"))
            if reservation is None or reservation.type_status == "conflict":
                record.reference_status = "unresolved"
                add_issue(
                    snapshot,
                    raw,
                    "unknown_reservation",
                    "Ссылка на бронь отсутствует или неоднозначна",
                )
            elif reservation.property_id != record.property_id:
                record.reference_status = "unresolved"
                add_issue(
                    snapshot, raw, "property_mismatch", "Объект платежа отличается от объекта брони"
                )
    known = {key for key, record in properties.items() if record.type_status == "valid"}
    for issue in snapshot.issues:
        if issue.property_id not in known:
            issue.property_id = None
