"""Read snapshot-bound quality provenance with SQL authorization before returning data."""

import json
from typing import Any
from uuid import UUID

from src.analytics.helper import ClickHouseHelper
from src.services.imports.records import Issue

JSON_FIELDS = {"actual_values", "related_entities", "effects"}
ISSUE_FIELDS = (
    "code",
    "severity",
    "source",
    "row_number",
    "business_key",
    "property_id",
    "related_rows",
    "actual_values",
    "action",
    "description",
    "recommendation",
    "impact",
    "inclusion",
    "related_entities",
    "period_start",
    "period_end",
    "effects",
    "is_global",
)


class QualityRepo:
    """Keep analytical quality SQL parameterized and scoped to one published run."""

    def __init__(self, helper: ClickHouseHelper) -> None:
        """Reuse the existing helper without acquiring per-row database clients."""
        self.helper = helper

    async def issues(self, run_id: UUID, ids: tuple[str, ...] | None) -> list[tuple[str, Issue]]:
        """Read only authorized primary issues; service also restricts related/history rows."""
        client = await self.helper.get_client()
        clause = " AND property_id IN {ids:Array(String)}" if ids is not None else ""
        result = await client.query(
            "SELECT issue_id,"
            + ",".join(ISSUE_FIELDS)
            + " FROM quality_issues WHERE run_id={run:UUID}"
            + clause
            + " ORDER BY source,row_number,code,issue_id",
            parameters={"run": run_id, "ids": list(ids or ())},
        )
        output = []
        for identity, *values in result.result_rows:
            data = dict(zip(ISSUE_FIELDS, values, strict=True))
            for field in JSON_FIELDS:
                data[field] = json.loads(data[field])
            data["is_global"] = bool(data["is_global"])
            output.append((identity, Issue(**data)))
        return output

    async def visible_rows(self, run_id: UUID, ids: tuple[str, ...]) -> set[tuple[str, int]]:
        """Authorize original raw versions independently of their current canonical object."""
        client = await self.helper.get_client()
        result = await client.query(
            "SELECT source,row_number FROM raw_rows WHERE run_id={run:UUID} AND JSONExtractString(payload,'property_id') IN {ids:Array(String)}",
            parameters={"run": run_id, "ids": list(ids)},
        )
        return set(result.result_rows)

    async def raw(
        self, run_id: UUID, source: str, rows: list[int], ids: tuple[str, ...] | None
    ) -> list[dict[str, Any]]:
        """Read explicitly referenced source rows after rechecking original object attribution."""
        client = await self.helper.get_client()
        clause = (
            " AND JSONExtractString(payload,'property_id') IN {ids:Array(String)}"
            if ids is not None
            else ""
        )
        result = await client.query(
            "SELECT row_number,business_key,payload,original_text FROM raw_rows WHERE run_id={run:UUID} AND source={source:String} AND row_number IN {rows:Array(UInt32)}"
            + clause
            + " ORDER BY row_number",
            parameters={"run": run_id, "source": source, "rows": rows, "ids": list(ids or ())},
        )
        return [
            {
                "source": source,
                "row_number": n,
                "business_key": key,
                "values": json.loads(payload),
                "original_text": original,
            }
            for n, key, payload, original in result.result_rows
        ]

    async def identities(self, run_id: UUID) -> dict[str, dict[str, str]]:
        """Read admin-only business hashes for comparison without asserting source deletions."""
        client = await self.helper.get_client()
        result = await client.query(
            "SELECT source,business_key,payload_hash FROM canonical_records WHERE run_id={run:UUID}",
            parameters={"run": run_id},
        )
        output: dict[str, dict[str, str]] = {}
        for source, key, value in result.result_rows:
            output.setdefault(source, {})[key] = value
        return output
