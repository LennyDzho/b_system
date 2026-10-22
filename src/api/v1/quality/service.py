"""Authorize diagnostic queries and source provenance for one active snapshot."""

import logging
from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.quality import QualityRepo
from src.api.v1.auth.schemas import Identity
from src.api.v1.finance.service import DashboardService
from src.api.v1.quality.schemas import QualityFilters, QualityResponse
from src.core.infra.exceptions import AuthError, DashboardError
from src.quality.diagnostics import issue_counts
from src.services.imports.records import SCHEMAS, Issue

logger = logging.getLogger(__name__)


def public_issue(identity: str, issue: Issue, run_id: UUID) -> dict[str, Any]:
    """Serialize diagnostic dates and exact source money without inventing financial values."""
    data = asdict(issue)
    data.update(issue_id=identity, run_id=str(run_id))
    for field in ("period_start", "period_end"):
        data[field] = data[field].isoformat() if data[field] else None
    data["is_limitation"] = (
        issue.is_global or issue.period_start is None or issue.period_end is None
    )
    return data


class QualityService:
    """Reuse the dashboard run resolver and restrict every diagnostic relationship to current rights."""

    def __init__(
        self, session: AsyncSession, dashboard: DashboardService, analytics: QualityRepo
    ) -> None:
        """Bind request-owned resources; do not create a second canonical or monetary pipeline."""
        self.session, self.dashboard, self.analytics = session, dashboard, analytics

    async def scoped_issues(
        self, run_id: UUID, ids: tuple[str, ...] | None
    ) -> list[tuple[str, Issue]]:
        """Restrict primary raw history, related entities and financial decisions before counting."""
        issues = await self.analytics.issues(run_id, ids)
        if ids is None:
            return issues
        visible = await self.analytics.visible_rows(run_id, ids)
        output = []
        for identity, issue in issues:
            if (issue.source, issue.row_number) not in visible:
                continue
            issue.related_rows = [n for n in issue.related_rows if (issue.source, n) in visible]
            related = []
            for entity in issue.related_entities:
                if entity.get("property_id") not in ids:
                    continue
                entity["source_rows"] = [
                    n for n in entity["source_rows"] if (entity["source"], n) in visible
                ]
                if entity["source_rows"]:
                    related.append(entity)
            issue.related_entities = related
            facts = []
            for fact in issue.effects.get("facts", []):
                if fact.get("property_id") in ids:
                    fact["source_rows"] = [
                        n for n in fact["source_rows"] if (fact["source"], n) in visible
                    ]
                    facts.append(fact)
            if issue.effects:
                issue.effects["facts"] = facts
            output.append((identity, issue))
        return output

    async def listing(
        self,
        user: Identity,
        filters: QualityFilters,
        property_id: str | None = None,
        run_id: UUID | None = None,
    ) -> QualityResponse:
        """Apply filters after SQL scope and before pagination/counters, retaining undated limits."""
        if filters.date_from and filters.date_to and filters.date_from > filters.date_to:
            raise AuthError(422, "Начальная дата должна быть не позже конечной")
        try:
            run, ids, properties = await self.dashboard.resolve(user, property_id, run_id)
            result = QualityResponse(
                state="no_snapshot",
                limit=filters.limit,
                offset=filters.offset,
                properties=properties,
            )
            if run is None:
                result.notices = ["Нет опубликованного снимка. Выполните CLI-импорт."]
                return result
            issues = await self.scoped_issues(run.run_id, ids)
            result.state, result.run_id = "ready", str(run.run_id)
            result.pipeline_version, result.calculation_version = (
                run.pipeline_version,
                run.calculation_version,
            )
            result.scope_counts = issue_counts([issue for _, issue in issues])
            result.facets = {
                field: sorted({getattr(issue, field) for _, issue in issues})
                for field in ("source", "code", "severity", "action")
            }
            selected = []
            for identity, issue in issues:
                if any(
                    getattr(filters, field) is not None
                    and getattr(issue, field) != getattr(filters, field)
                    for field in ("source", "code", "action", "severity")
                ):
                    continue
                if (
                    filters.business_key is not None
                    and issue.business_key != filters.business_key
                    and not any(
                        entity["business_key"] == filters.business_key
                        for entity in issue.related_entities
                    )
                ):
                    continue
                limitation = (
                    issue.is_global or issue.period_start is None or issue.period_end is None
                )
                if (
                    filters.section == "limitations"
                    and not limitation
                    or filters.section == "period"
                    and limitation
                ):
                    continue
                if not limitation and (
                    filters.date_from
                    and issue.period_end < filters.date_from
                    or filters.date_to
                    and issue.period_start > filters.date_to
                ):
                    continue
                selected.append((identity, issue))
            result.total = len(selected)
            result.filtered_counts = issue_counts([issue for _, issue in selected])
            result.items = [
                public_issue(identity, issue, run.run_id)
                for identity, issue in selected[filters.offset : filters.offset + filters.limit]
            ]
            result.notices = [
                "Счётчики относятся только к доступным объектам. Проблемы, физические строки, сущности и финансовые исключения считаются отдельно.",
                "Глобальные проблемы и строки без достоверной даты не скрываются периодом: они отмечены как ограничения снимка; доступны отдельным разделом.",
                "Overlap фильтруется по общим ночам; остальные датированные проблемы — по дате своего факта. Нет платежа в выгрузке не означает просрочку.",
            ]
            if not run.pipeline_version.startswith("quality-"):
                result.notices.append(
                    "Старый снимок: расширенная диагностика E05 отсутствует; сохранённые проблемы показаны без пересчёта и изменения истории."
                )
            return result
        except AuthError:
            raise
        except Exception as error:
            logger.error("Quality backend failure: %s", type(error).__name__)
            raise DashboardError(
                "Диагностика недоступна: не удалось прочитать базы данных."
            ) from None

    async def detail(
        self, user: Identity, identity: str, run_id: UUID | None = None
    ) -> dict[str, Any]:
        """Authorize before exposing raw payload, related records or stored financial effects."""
        try:
            run, ids, _ = await self.dashboard.resolve(user, None, run_id)
            if run is None:
                raise AuthError(404, "Проблема не найдена")
            issue = next(
                (i for key, i in await self.scoped_issues(run.run_id, ids) if key == identity), None
            )
            if issue is None:
                raise AuthError(404, "Проблема не найдена в доступных объектах")
            related = []
            for entity in issue.related_entities:
                related.append(
                    {
                        **entity,
                        "raw": await self.analytics.raw(
                            run.run_id, entity["source"], entity["source_rows"], ids
                        ),
                    }
                )
            return {
                "run_id": str(run.run_id),
                "issue": public_issue(identity, issue, run.run_id),
                "raw": await self.analytics.raw(
                    run.run_id, issue.source, sorted({issue.row_number, *issue.related_rows}), ids
                ),
                "related": related,
            }
        except AuthError:
            raise
        except Exception as error:
            logger.error("Quality detail failure: %s", type(error).__name__)
            raise DashboardError(
                "Диагностика недоступна: не удалось прочитать базы данных."
            ) from None

    async def raw_detail(
        self, user: Identity, source: str, row: int, run_id: UUID | None = None
    ) -> dict[str, Any]:
        """Support source-only entities/rules and malformed IDs without bypassing original row scope."""
        if source not in SCHEMAS:
            raise AuthError(404, "Источник не найден")
        if not 1 <= row <= 4294967295:
            raise AuthError(422, "Номер исходной строки вне допустимого диапазона")
        try:
            run, ids, _ = await self.dashboard.resolve(user, None, run_id)
            rows = await self.analytics.raw(run.run_id, source, [row], ids) if run else []
            if not rows:
                raise AuthError(404, "Исходная строка не найдена в доступных объектах")
            return {"run_id": str(run.run_id), "rows": rows}
        except AuthError:
            raise
        except Exception as error:
            logger.error("Source detail failure: %s", type(error).__name__)
            raise DashboardError("Исходные данные недоступны.") from None
