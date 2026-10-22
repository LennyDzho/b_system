"""Serve one authorized published financial snapshot consistently to API and Jinja screens."""

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.dashboard import DashboardRepo
from src.api.v1.auth.schemas import Identity
from src.api.v1.auth.service import scope_for
from src.api.v1.finance.schemas import DashboardCharts, DashboardResponse, MoneyMetric
from src.core.infra.exceptions import AuthError, DashboardError
from src.database.models import ImportRun
from src.database.repositories.imports import ImportRepo
from src.finance.policy import CALCULATION_VERSION, LABELS, aggregate, fact_dict
from src.finance.series import period_series

logger = logging.getLogger(__name__)


class DashboardService:
    """Pin the PG run once; reuse that ID for scope, facts and provenance."""

    def __init__(self, session: AsyncSession, analytics: DashboardRepo) -> None:
        """Bind request-local PG state and the dedicated analytical repository."""
        self.session = session
        self.analytics = analytics

    async def resolve(
        self, user: Identity, property_id: str | None, run_id: UUID | None
    ) -> tuple[ImportRun | None, tuple[str, ...] | None, list[dict[str, Any]]]:
        """Authorize before existence checks and resolve exactly one published snapshot."""
        scope = scope_for(user, property_id)
        selected = run_id if run_id is not None else await ImportRepo(self.session).active_id()
        if selected is None:
            return None, scope.property_ids, []
        run = await self.session.get(ImportRun, selected)
        if run is None or run.status != "published":
            raise AuthError(404, "Опубликованный снимок не найден")
        properties = await self.analytics.properties(selected, scope.property_ids)
        known = tuple(sorted(p["property_id"] for p in properties))
        if property_id and property_id not in known:
            raise AuthError(404, "Объект отсутствует в выбранном снимке")
        return run, None if scope.property_ids is None else known, properties

    async def dashboard(
        self,
        user: Identity,
        property_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        run_id: UUID | None = None,
    ) -> DashboardResponse:
        """Build exact totals and per-property rows; report missing data without invented zeros."""
        if date_from and date_to and date_from > date_to:
            raise AuthError(422, "Начальная дата должна быть не позже конечной")
        try:
            run, ids, properties = await self.resolve(user, property_id, run_id)
            result = DashboardResponse(
                state="no_snapshot",
                date_from=date_from.isoformat() if date_from else None,
                date_to=date_to.isoformat() if date_to else None,
            )
            if run is None:
                result.warnings = ["Нет опубликованного снимка. Выполните CLI-импорт данных."]
                return result
            result.run_id, result.calculation_version, result.pipeline_version = (
                str(run.run_id),
                run.calculation_version,
                run.pipeline_version,
            )
            result.imported_at = run.finished_at.isoformat() if run.finished_at else None
            result.scope = [p["property_id"] for p in properties]
            result.properties = properties
            if run.calculation_version != CALCULATION_VERSION:
                result.state = "not_calculated"
                result.warnings = [
                    "Для этого снимка поддерживаемый финансовый расчёт отсутствует. Выполните новый CLI-импорт."
                ]
                return result
            all_facts = await self.analytics.facts(run.run_id, ids)
            dates = [fact.fact_date for fact in all_facts if fact.fact_date is not None]
            if dates:
                if date_from is None:
                    date_from = min(min(dates), date_to) if date_to else min(dates)
                if date_to is None:
                    date_to = max(max(dates), date_from)
            result.date_from = date_from.isoformat() if date_from else None
            result.date_to = date_to.isoformat() if date_to else None
            facts = await self.analytics.facts(run.run_id, ids, date_from, date_to)
            totals = aggregate(facts)
            for key, value in totals.items():
                setattr(
                    result,
                    key,
                    {name: MoneyMetric(**metric) for name, metric in value.items()}
                    if key == "metrics"
                    else value,
                )
            result.records = [fact_dict(f) for f in facts]
            result.state = "ready" if facts else "empty"
            if date_from and date_to and any(f.fact_date is not None for f in facts):
                result.charts = DashboardCharts(
                    trend=period_series(facts, date_from, date_to),
                    cash_accrual=period_series(facts, date_from, date_to, comparison=True),
                )
            result.properties = [
                {**prop, **aggregate([f for f in facts if f.property_id == prop["property_id"]])}
                for prop in properties
            ]
            result.warnings = [
                "Оценочный результат собственника за период — неполная оценка до неизвестных комиссий, не остаток денег и не сумма к выплате."
            ]
            if result.unknown_fees or any(v["excluded"] for v in result.coverage.values()):
                result.warnings.append(
                    "До уточнения management fees по отменам и другим правилам; часть некорректных записей исключена. Причины и покрытие указаны ниже."
                )
            if any(f.fact_date is None for f in facts):
                result.snapshot_limitations.append(
                    "Записи без достоверной финансовой даты показаны отдельно: период к ним не применён, в суммы они не включены."
                )
            if user.role == "admin" and property_id is None:
                unallocated: dict[tuple[str, str], list[Any]] = defaultdict(list)
                for fact in facts:
                    if fact.property_id is None or "PROPERTY_MISMATCH" in fact.reasons:
                        unallocated[fact.source, fact.currency].append(fact)
                for (source, currency), group in unallocated.items():
                    result.unallocated.append(
                        {
                            "source": source,
                            "currency": currency,
                            "count": len(group),
                            "records": [fact_dict(fact) for fact in group],
                            "observed_amount": format(
                                sum((f.amount for f in group if f.amount is not None), Decimal(0)),
                                "f",
                            )
                            if source != "reservations.csv"
                            else None,
                        }
                    )
            return result
        except AuthError:
            raise
        except Exception as error:
            logger.error("Dashboard backend failure: %s", type(error).__name__)
            raise DashboardError() from None

    async def detail(
        self, user: Identity, source: str, key: str, run_id: UUID | None = None
    ) -> dict[str, Any]:
        """Authorize the entity before reading raw values, formulas, rules and related cash."""
        if source not in {"reservations.csv", "payments.csv", "expenses.csv"}:
            raise AuthError(404, "Источник не найден")
        try:
            run, ids, _ = await self.resolve(user, None, run_id)
            if run is None or run.calculation_version != CALCULATION_VERSION:
                raise AuthError(404, "Рассчитанный снимок не найден")
            facts = await self.analytics.facts(run.run_id, ids)
            fact = next((f for f in facts if f.source == source and f.business_key == key), None)
            if fact is None:
                raise AuthError(404, "Запись не найдена в доступных объектах")
            provenance = await self.analytics.provenance(
                run.run_id, source, key, ids, fact.source_rows
            )
            payments = []
            if source == "reservations.csv":
                # Cash is aggregated independently; no booking×payment join affects totals.
                for payment in facts:
                    if payment.source == "payments.csv" and payment.property_id == fact.property_id:
                        item = await self.analytics.provenance(
                            run.run_id,
                            payment.source,
                            payment.business_key,
                            ids,
                            payment.source_rows,
                        )
                        if any(c["values"].get("reservation_id") == key for c in item["canonical"]):
                            payments.append(fact_dict(payment))
            return {
                "run_id": str(run.run_id),
                "calculation_version": run.calculation_version,
                "currency": fact.currency,
                "fact": fact_dict(fact),
                "summary": aggregate([fact]),
                **provenance,
                "payments": payments,
                "commission_rules": await self.analytics.rules(run.run_id, fact.property_id)
                if fact.property_id
                else [],
                "reason_descriptions": {code: LABELS.get(code, code) for code in fact.reasons},
                "formulas": [
                    "Дата начисления = check_out; выбор правила = check_in; расход = expense_date; cash = payment_date",
                    "Net = gross − refund; после OTA = gross − refund − OTA",
                    "Management = ROUND_HALF_UP(gross × rate, 1 IDR); при неизвестной политике NULL",
                    "Оценочный вклад до расходов = gross − refund − OTA − known management; неизвестная management не считается подтверждённым нулём",
                    "Owner estimate периода = начисления после OTA − known management − paid/pending расходы",
                    "Cash = сумма самостоятельных received payment.amount; OTA и refund повторно не вычитаются",
                ],
            }
        except AuthError:
            raise
        except Exception as error:
            logger.error("Detail backend failure: %s", type(error).__name__)
            raise DashboardError() from None
