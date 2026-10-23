"""Render session-backed screens without importing financial data or logic."""

from dataclasses import replace
from typing import Annotated, Literal
from uuid import UUID

from dishka.integrations.fastapi import FromDishka
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.v1.auth.schemas import AccessScope, Identity
from src.api.v1.auth.service import AuthService
from src.api.v1.finance.dependencies import dashboard_data
from src.api.v1.finance.schemas import DashboardResponse
from src.api.v1.finance.service import DashboardService
from src.core.infra.exceptions import AuthError, DashboardError
from src.core.settings import AuthConfig
from src.guards.session import access_scope, current_user
from web.presentation import METRICS, SOURCES, dashboard_url, operations, record_url, return_url
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
    service: FromDishka[DashboardService],
    view: Literal["overview", "properties", "operations"] = "overview",
    metric: Literal[
        "gross",
        "refund",
        "net",
        "ota",
        "management",
        "expenses",
        "owner_estimate",
        "cash",
        "after_ota",
        "accrual_cash_difference",
    ]
    | None = None,
    source: Literal["reservations.csv", "payments.csv", "expenses.csv"] | None = None,
    record_query: Annotated[str, Query(max_length=128)] = "",
    status: Literal["all", "included", "excluded"] = "all",
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Render one snapshot with server-side view selection and authorized operation pagination."""
    from src.finance.policy import LABELS

    property_options = data.properties
    if data.run_id and request.query_params.get("property_id"):
        try:
            _, _, property_options = await service.resolve(user, None, UUID(data.run_id))
        except AuthError:
            raise
        except Exception:
            raise DashboardError() from None

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "data": data,
            "user": user,
            "labels": METRICS,
            "source_labels": SOURCES,
            "property_options": property_options,
            "view": view,
            "metric": metric,
            "operations": operations(data.records, metric, source, record_query, status, page),
            "dashboard_url": lambda **changes: dashboard_url(
                request, **{"run_id": data.run_id, **changes}
            ),
            "record_url": lambda source, key: record_url(
                source, key, data.run_id or "", dashboard_url(request, run_id=data.run_id)
            ),
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
    back: Annotated[str | None, Query(max_length=2048)] = None,
) -> HTMLResponse:
    """Render authorized raw provenance and financial explanations for a pinned snapshot."""
    data = await service.detail(user, source + ".csv", key, run_id)
    return templates.TemplateResponse(
        request,
        "financial_detail.html",
        {
            "data": data,
            "user": user,
            "back_url": return_url(back, data["run_id"]),
            "record_url": lambda source, key: record_url(
                source, key, data["run_id"], return_url(back, data["run_id"])
            ),
        },
        headers={"Cache-Control": "no-store"},
    )
