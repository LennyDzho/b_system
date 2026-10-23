"""Build safe navigation and paginate already authorized facts without calculating money."""

from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

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
