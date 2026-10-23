"""Protect financial drill-down selection, safe return context and native HTML filters."""

from copy import deepcopy
from datetime import date
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from dishka import make_async_container
from dishka.integrations.fastapi import setup_dishka
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from web.presentation import operations, return_url
from web.routing import HtmlRoute


def test_metric_drill_down_keeps_unknowns_and_explains_both_cash_flows() -> None:
    """Hide confirmed zero refunds, retain unknowns/exclusions and never rewrite source amounts."""
    records = [
        {
            "source": source,
            "business_key": key,
            "property_id": "V019",
            "included": included,
            "refund": refund,
            "amount": "728127.00",
        }
        for source, key, included, refund in (
            ("reservations.csv", "R0", True, "0"),
            ("reservations.csv", "R1", True, "100.50"),
            ("reservations.csv", "R2", False, None),
            ("payments.csv", "P1", True, None),
            ("expenses.csv", "E1", True, None),
        )
    ]
    original = deepcopy(records)
    refunds = operations(records, "refund", None, "", "all", 1)
    assert [row["business_key"] for row in refunds["items"]] == ["R1", "R2"]
    cash = operations(records, "accrual_cash_difference", None, "", "all", 1)
    assert {row["source"] for row in cash["items"]} == {"reservations.csv", "payments.csv"}
    owner = operations(records, "owner_estimate", None, "", "all", 1)
    assert "E1" in {row["business_key"] for row in owner["items"]}
    assert "P1" not in {row["business_key"] for row in owner["items"]}
    assert records == original


def test_operation_pages_preserve_scope_search_and_exclusions() -> None:
    """Page only supplied authorized rows and clamp stale links after narrowing the search."""
    rows = [
        {
            "source": "expenses.csv",
            "business_key": f"E{i:03}",
            "property_id": "V019",
            "included": i % 2 == 0,
        }
        for i in range(63)
    ]
    pages = [operations(rows, None, None, "", "all", page) for page in (1, 2, 3)]
    assert [len(page["items"]) for page in pages] == [25, 25, 13]
    assert len({row["business_key"] for page in pages for row in page["items"]}) == 63
    narrowed = operations(rows, "expenses", "expenses.csv", "v019", "excluded", 999)
    assert narrowed["page"] == 2 and narrowed["total"] == 31
    assert all(not row["included"] for row in narrowed["items"])
    assert operations(rows, None, None, "foreign-object", "all", 1)["total"] == 0


def test_detail_return_context_is_local_allowlisted_and_snapshot_pinned() -> None:
    """Reject foreign/malformed destinations and keep object/date/page context on a local return."""
    for destination in (
        "https://evil.test/dashboard",
        "//evil.test/dashboard",
        "/account",
        "//[bad",
    ):
        assert return_url(destination, "published-run") == "/dashboard?run_id=published-run"
    result = return_url(
        "/dashboard?run_id=old&property_id=V019&date_from=2026-09-01&view=operations&page=2&secret=x",
        "published-run",
    )
    params = parse_qs(urlsplit(result).query)
    assert params == {
        "run_id": ["published-run"],
        "property_id": ["V019"],
        "date_from": ["2026-09-01"],
        "view": ["operations"],
        "page": ["2"],
    }


def test_native_blank_html_controls_keep_shared_validation_and_api_contracts() -> None:
    """Support no-JS GET forms without accepting malformed nonempty inputs or changing API rules."""
    application = FastAPI()
    setup_dishka(make_async_container(), application)
    router = APIRouter(route_class=HtmlRoute)

    @router.get("/html")
    async def html(
        source: Literal["expenses.csv"] | None = None, date_from: date | None = None
    ) -> dict[str, str | None]:
        """Use ordinary typed filter parameters, just like the real HTML domain routes."""
        return {"source": source, "date_from": date_from.isoformat() if date_from else None}

    @application.get("/api")
    async def api(date_from: date | None = None) -> dict[str, str | None]:
        """Keep API validation separate from browser form normalization."""
        return {"date_from": date_from.isoformat() if date_from else None}

    application.include_router(router)
    with TestClient(application) as client:
        assert client.get("/html?source=&date_from=").json() == {"source": None, "date_from": None}
        assert client.get("/html?source=expenses.csv&date_from=2026-09-01").status_code == 200
        assert client.get("/html?date_from=bad").status_code == 422
        assert client.get("/html?source=unknown").status_code == 422
        assert client.get("/api?date_from=").status_code == 422
