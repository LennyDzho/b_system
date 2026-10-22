"""Write and verify immutable ClickHouse snapshots through a dedicated repository."""

from dataclasses import asdict, fields
from typing import Any
from uuid import UUID

from src.analytics.helper import ClickHouseHelper
from src.core.infra.exceptions import SnapshotMismatch
from src.finance.policy import FinancialFact
from src.services.imports.records import SCHEMAS, TYPED_FIELDS, Snapshot, digest, json_text

TEXT_FIELDS = (
    "property_name",
    "cluster",
    "owner_id",
    "base_currency",
    "reservation_id",
    "currency",
    "status",
    "payment_status",
    "category",
    "channel",
    "fee_basis",
    "note",
)
TABLE_KEYS = {
    "raw_rows": ("source", "row_number"),
    "canonical_records": ("source", "business_key"),
    "quality_issues": ("issue_id",),
    "financial_facts": ("source", "business_key"),
}


class AnalyticsImportRepo:
    """Keep all analytical SQL and fixed table/column allowlists outside services."""

    def __init__(self, helper: ClickHouseHelper) -> None:
        """Reuse the lifespan/CLI-owned native async client."""
        self.helper = helper

    def rows(
        self, run_id: UUID, snapshot: Snapshot
    ) -> dict[str, tuple[list[str], list[list[Any]]]]:
        """Encode original values and typed nullable columns without financial zeros."""
        raw_columns = [
            "run_id",
            "source",
            "row_number",
            "business_key",
            "payload_hash",
            "payload",
            "original_text",
        ]
        raw = [
            [
                run_id,
                row.source,
                row.row_number,
                row.business_key,
                row.payload_hash,
                json_text(row.payload),
                row.original_text,
            ]
            for row in snapshot.raw
        ]
        canonical_columns = [
            "run_id",
            "source",
            "business_key",
            "property_id",
            "source_rows",
            "payload_hash",
            "payload",
            "type_status",
            "reference_status",
            *TEXT_FIELDS,
            *TYPED_FIELDS,
        ]
        canonical = [
            [
                run_id,
                row.source,
                row.business_key,
                row.property_id,
                row.source_rows,
                row.payload_hash,
                json_text(row.payload),
                row.type_status,
                row.reference_status,
                *[row.payload.get(name) for name in TEXT_FIELDS],
                *[row.typed.get(name) for name in TYPED_FIELDS],
            ]
            for row in snapshot.canonical
        ]
        issue_columns = [
            "run_id",
            "issue_id",
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
        ]
        issues = []
        for issue in snapshot.issues:
            identity = digest(json_text(asdict(issue)).encode())
            issues.append(
                [
                    run_id,
                    identity,
                    issue.code,
                    issue.severity,
                    issue.source,
                    issue.row_number,
                    issue.business_key,
                    issue.property_id,
                    issue.related_rows,
                    json_text(issue.actual_values),
                    issue.action,
                    issue.description,
                    issue.recommendation,
                    issue.impact,
                    issue.inclusion,
                    json_text(issue.related_entities),
                    issue.period_start,
                    issue.period_end,
                    json_text(issue.effects),
                    int(issue.is_global),
                ]
            )
        financial_columns = [field.name for field in fields(FinancialFact)]
        return {
            "raw_rows": (raw_columns, raw),
            "canonical_records": (canonical_columns, canonical),
            "quality_issues": (issue_columns, issues),
            "financial_facts": (
                ["run_id", *financial_columns],
                [
                    [
                        run_id,
                        *[
                            int(fact.included) if column == "included" else getattr(fact, column)
                            for column in financial_columns
                        ],
                    ]
                    for fact in snapshot.financial_facts
                ],
            ),
        }

    async def write(self, run_id: UUID, snapshot: Snapshot) -> None:
        """Insert once per table; uncertain writes are never blindly retried by the service."""
        client = await self.helper.get_client()
        for table, (columns, rows) in self.rows(run_id, snapshot).items():
            if rows:
                await client.insert(table, rows, column_names=columns, settings={"async_insert": 0})

    async def verify(self, run_id: UUID, snapshot: Snapshot) -> None:
        """Read back counts, keys, and every stored field before making a snapshot visible."""
        client = await self.helper.get_client()
        for table, (columns, expected) in self.rows(run_id, snapshot).items():
            keys = ",".join(TABLE_KEYS[table])
            totals = await client.query(
                f"SELECT count(), uniqExact(tuple({keys})) FROM {table} WHERE run_id = {{run_id:UUID}}",
                parameters={"run_id": run_id},
            )
            if totals.result_rows != [(len(expected), len(expected))]:
                raise SnapshotMismatch(f"Count or duplicate key mismatch in {table}")
            result = await client.query(
                f"SELECT {','.join(columns)} FROM {table} WHERE run_id = {{run_id:UUID}}",
                parameters={"run_id": run_id},
            )
            # Compare whole read-back payloads; this also covers nullable Decimal precision.
            if sorted(json_text(list(row)) for row in result.result_rows) != sorted(
                json_text(row) for row in expected
            ):
                raise SnapshotMismatch(f"Read-back content mismatch in {table}")

    async def fingerprints(self, run_id: UUID) -> dict[str, dict[str, str]]:
        """Read business payload identities to describe changes without asserting deletions."""
        client = await self.helper.get_client()
        result = await client.query(
            "SELECT source, business_key, payload_hash FROM canonical_records WHERE run_id = {run_id:UUID}",
            parameters={"run_id": run_id},
        )
        output = {name: {} for name in SCHEMAS}
        for source, key, payload_hash in result.result_rows:
            output[source][key] = payload_hash
        return output

    async def property_ids(self, run_id: UUID) -> frozenset[str]:
        """Read the published reference dictionary, without claiming financial eligibility."""
        client = await self.helper.get_client()
        result = await client.query(
            "SELECT business_key FROM canonical_records WHERE run_id = {run_id:UUID} AND source = 'properties.csv' AND type_status = 'valid'",
            parameters={"run_id": run_id},
        )
        return frozenset(row[0] for row in result.result_rows)
