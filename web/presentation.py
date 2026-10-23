"""Build safe navigation and paginate already authorized facts without calculating money."""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from fastapi import Request

SOURCES = {
    "reservations.csv": "Бронирования",
    "payments.csv": "Поступления",
    "expenses.csv": "Расходы",
    "properties.csv": "Объекты",
    "commission_rules.csv": "Правила комиссий",
}
METRICS = {
    "gross": "Стоимость бронирований",
    "refund": "Возвраты",
    "net": "После возвратов",
    "ota": "Комиссии каналов",
    "management": "Комиссия управления",
    "expenses": "Операционные расходы",
    "owner_estimate": "Результат собственника",
    "cash": "Поступления",
    "after_ota": "Начисления после OTA",
    "accrual_cash_difference": "Разница начислений и поступлений",
}
LABELS = {
    **SOURCES,
    "published": "Опубликован",
    "failed": "Ошибка",
    "running": "Выполняется",
    "admin": "Администратор",
    "viewer": "Наблюдатель",
    "warning": "Предупреждение",
    "error": "Ошибка",
    "info": "Информация",
    "included": "Включено",
    "excluded": "Исключено",
    "excluded_raw": "Повторная или прежняя строка исключена",
    "not_calculated": "Без финансового расчёта",
    "not_applicable": "Не применяется",
    "preserve_financial_decision": "Сохранено решение по суммам",
    "exclude_duplicate": "Повтор исключён",
    "exclude_superseded": "Сохранена последняя версия",
    "review": "Требует уточнения",
    "exclude_financial": "Сумма исключена",
    "include_with_warning": "Включено с предупреждением",
    "retain_unknown": "Сохранена неизвестность",
    "reconciled": "Сверено",
    "unreconciled": "Не сверено",
    "see_financial_decisions": "Решение отдельно по каждому показателю",
    "occupancy": "Проживание",
    "accrual": "Начисления",
    "cash": "Поступления",
    "cash_reconciliation": "Сверка поступлений",
    "OCCUPANCY_OVERLAP": "Пересечение проживаний",
    "DIRECT_WITH_OTA": "Комиссия OTA у Direct",
    "ZERO_RECEIVED_PAYMENT": "Поступление с нулевой суммой",
    "PAYMENT_BOOKING_CURRENCY": "Валюты платежа и брони различаются",
    "PAYMENTS_VS_ACCRUAL": "Поступления отличаются от начислений",
    "UNRECONCILED_PAYMENT": "Поступление не сверено",
    "duplicate": "Повтор исходной строки",
    "superseded": "Прежняя версия записи",
}
DASHBOARD_KEYS = frozenset(
    {
        "run_id",
        "property_id",
        "date_from",
        "date_to",
        "view",
        "metric",
        "source",
        "record_query",
        "status",
        "page",
    }
)
PAGE_SIZE = 25


def human_date(value: str | date | datetime | None) -> str:
    """Display source dates consistently and label audit timestamps explicitly as UTC."""
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        value = value.astimezone(UTC) if value.tzinfo else value
        return value.strftime("%d.%m.%Y, %H:%M") + (" UTC" if value.tzinfo else "")
    return value.strftime("%d.%m.%Y")


def label(value: str | None) -> str:
    """Translate known presentation terms while preserving unrecognized source codes."""
    return LABELS.get(value, value or "—")


def dashboard_url(request: Request, **updates: object) -> str:
    """Retain only dashboard filters, with explicit resets represented by None."""
    params = {key: value for key, value in request.query_params.items() if key in DASHBOARD_KEYS}
    for key, value in updates.items():
        if value is None or value == "":
            params.pop(key, None)
        elif key in DASHBOARD_KEYS:
            params[key] = str(value)
    return "/dashboard" + ("?" + urlencode(params) if params else "")


def return_url(value: str | None, run_id: str) -> str:
    """Accept only local dashboard navigation and pin it to the authorized detail snapshot."""
    try:
        parsed = urlsplit(value or "")
    except ValueError:
        parsed = urlsplit("")
    params = (
        {key: text for key, text in parse_qsl(parsed.query) if key in DASHBOARD_KEYS}
        if not parsed.scheme and not parsed.netloc and parsed.path == "/dashboard"
        else {}
    )
    params["run_id"] = run_id
    return "/dashboard?" + urlencode(params)


def record_url(source: str, key: str, run_id: str, back: str) -> str:
    """Carry a safe return context through financial detail links without changing API contracts."""
    return (
        f"/detail/{quote(source.removesuffix('.csv'), safe='')}/{quote(key, safe='')}?"
        + urlencode({"run_id": run_id, "back": back})
    )


def navigation(request: Request) -> dict[str, Any]:
    """Share active navigation and snapshot/date context across every HTML screen."""
    path = request.url.path
    context = {
        key: value
        for key, value in request.query_params.items()
        if key in {"run_id", "property_id", "date_from", "date_to"}
    }
    suffix = "?" + urlencode(context) if context else ""
    return {
        "finance_home": "/dashboard" + suffix,
        "quality_home": "/quality" + suffix,
        "active_section": (
            "finance"
            if path.startswith(("/dashboard", "/detail"))
            else "quality"
            if path.startswith("/quality")
            else "imports"
            if path.startswith("/imports")
            else "account"
        ),
    }


def operations(
    records: list[dict[str, Any]],
    metric: str | None,
    source: str | None,
    query: str,
    status: str,
    page: int,
) -> dict[str, Any]:
    """Filter existing decisions by identity/type and paginate; never aggregate or admit money.

    Refund/OTA drill-down hides confirmed zero components but retains unknown
    components for explanation. All other decisions, including exclusions, remain
    visible until an explicit status filter is applied. No monetary value is changed.
    """
    selected = []
    for row in records:
        if source and row["source"] != source:
            continue
        if status != "all" and row["included"] != (status == "included"):
            continue
        if (
            query.casefold()
            not in (row["business_key"] + " " + (row["property_id"] or "")).casefold()
        ):
            continue
        expected = (
            {"expenses.csv"}
            if metric == "expenses"
            else {"payments.csv"}
            if metric == "cash"
            else {"reservations.csv", "expenses.csv"}
            if metric == "owner_estimate"
            else {"reservations.csv", "payments.csv"}
            if metric == "accrual_cash_difference"
            else {"reservations.csv"}
            if metric
            else set(SOURCES)
        )
        if row["source"] not in expected:
            continue
        if metric in {"refund", "ota"} and row[metric] is not None and Decimal(row[metric]) == 0:
            continue
        selected.append(row)
    pages = max(1, (len(selected) + PAGE_SIZE - 1) // PAGE_SIZE)
    current = min(page, pages)
    start = (current - 1) * PAGE_SIZE
    return {
        "items": selected[start : start + PAGE_SIZE],
        "total": len(selected),
        "page": current,
        "pages": pages,
        "start": start + 1 if selected else 0,
        "end": min(start + PAGE_SIZE, len(selected)),
    }
