"""Render session-backed screens without importing financial data or logic."""

from dataclasses import replace
from typing import Annotated
from uuid import UUID

from dishka.integrations.fastapi import FromDishka
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.v1.auth.schemas import AccessScope, Identity
from src.api.v1.auth.service import AuthService
from src.api.v1.finance.dependencies import dashboard_data
from src.api.v1.finance.schemas import DashboardResponse
from src.api.v1.finance.service import DashboardService
from src.core.infra.exceptions import AuthError
from src.core.settings import AuthConfig
from src.guards.session import access_scope, current_user
from web.routing import HtmlRoute
from web.templating import templates

router = APIRouter(include_in_schema=False, route_class=HtmlRoute)


@router.get("/login", response_model=None)
async def login_page(
    request: Request, service: FromDishka[AuthService], config: FromDishka[AuthConfig]
) -> HTMLResponse | RedirectResponse:
    """Show credentials input or redirect an already authenticated user."""
    token = request.cookies.get(config.cookie_name)
    if token:
        try:
            await service.identity(token)
        except AuthError as exception:
            if exception.status_code != 401:
                raise
        else:
            return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", headers={"Cache-Control": "no-store"})


@router.get("/account")
async def account_page(
    request: Request, scope: Annotated[AccessScope, Depends(access_scope)]
) -> HTMLResponse:
    """Show the current identity and a protected logout control."""
    user = replace(scope.user, property_ids=frozenset(scope.property_ids or ()))
    return templates.TemplateResponse(
        request, "account.html", {"user": user}, headers={"Cache-Control": "no-store"}
    )


@router.get("/dashboard")
async def dashboard_page(
    request: Request,
    data: Annotated[DashboardResponse, Depends(dashboard_data)],
    user: Annotated[Identity, Depends(current_user)],
) -> HTMLResponse:
    """Render the same server-calculated metrics and scope as the protected API."""
    labels = {
        "gross": "Стоимость бронирований · Gross",
        "refund": "Возвраты · корректировки",
        "net": "После возвратов · Net",
        "ota": "Комиссии каналов · OTA",
        "management": "Комиссии управления · Management",
        "expenses": "Операционные расходы",
        "owner_estimate": "Оценочный результат собственника за период",
        "cash": "Зарегистрированные поступления",
        "after_ota": "Начисления после OTA",
        "accrual_cash_difference": "Разница начислений и поступлений",
    }
    from src.finance.policy import LABELS

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "data": data,
            "user": user,
            "labels": labels,
            "reason_labels": LABELS,
            "chart_payload": {
                "run_id": data.run_id,
                "charts": data.charts.model_dump(mode="json") if data.charts else None,
                "metrics": {key: metric.model_dump() for key, metric in data.metrics.items()},
                "properties": [
                    {key: prop[key] for key in ("property_id", "property_name", "metrics")}
                    for prop in data.properties
                    if "metrics" in prop
                ],
            },
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/detail/{source}/{key}")
async def detail_page(
    request: Request,
    source: str,
    key: str,
    user: Annotated[Identity, Depends(current_user)],
    service: FromDishka[DashboardService],
    run_id: UUID | None = None,
) -> HTMLResponse:
    """Render authorized raw provenance and financial explanations for a pinned snapshot."""
    data = await service.detail(user, source + ".csv", key, run_id)
    return templates.TemplateResponse(
        request,
        "financial_detail.html",
        {"data": data, "user": user},
        headers={"Cache-Control": "no-store"},
    )
