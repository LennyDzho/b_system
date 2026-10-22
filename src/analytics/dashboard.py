"""Read parameterized, snapshot-bound analytical records without SQL joins that multiply money."""

import json
from dataclasses import fields
from datetime import date
from typing import Any
from uuid import UUID

from src.analytics.helper import ClickHouseHelper
from src.finance.policy import FinancialFact


class DashboardRepo:
    """Apply object and date restrictions before analytical rows leave ClickHouse."""

    def __init__(self, helper: ClickHouseHelper) -> None:
        """Reuse the application-owned analytical connection."""
        self.helper = helper

    async def properties(self, run_id: UUID, ids: tuple[str, ...] | None) -> list[dict[str, Any]]:
        """Return only visible dictionary entries for this exact published run."""
        client = await self.helper.get_client()
        clause = " AND business_key IN {ids:Array(String)}" if ids is not None else ""
        result = await client.query(
            "SELECT business_key, property_name, base_currency FROM canonical_records WHERE run_id={run:UUID} AND source='properties.csv' AND type_status='valid'"
            + clause,
            parameters={"run": run_id, "ids": list(ids or ())},
        )
        return [
            {"property_id": key, "property_name": name, "currency": currency}
            for key, name, currency in result.result_rows
        ]

    async def facts(
        self,
        run_id: UUID,
        ids: tuple[str, ...] | None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[FinancialFact]:
        """Read scoped facts using each source's own financial date, retaining undated limits."""
        columns = [field.name for field in fields(FinancialFact)]
        clauses = ["run_id={run:UUID}"]
        params: dict[str, Any] = {"run": run_id}
        if ids is not None:
            clauses.append("property_id IN {ids:Array(String)}")
            params["ids"] = list(ids)
        for name, value, op in (("start", date_from, ">="), ("end", date_to, "<=")):
            if value is not None:
                clauses.append(f"(fact_date IS NULL OR fact_date {op} {{{name}:Date32}})")
                params[name] = value
        client = await self.helper.get_client()
        result = await client.query(
            "SELECT "
            + ",".join(columns)
            + " FROM financial_facts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY source,business_key",
            parameters=params,
        )
        facts = [
            FinancialFact(**dict(zip(columns, row, strict=True))) for row in result.result_rows
        ]
        for fact in facts:
            fact.included = bool(fact.included)
        return facts

    async def provenance(
        self,
        run_id: UUID,
        source: str,
        key: str,
        ids: tuple[str, ...] | None = None,
        source_rows: list[int] | None = None,
    ) -> dict[str, Any]:
        """Read canonical/raw/issues only after the service has authorized the source entity."""
        client = await self.helper.get_client()
        params = {
            "run": run_id,
            "source": source,
            "key": key,
            "ids": list(ids or ()),
            "rows": source_rows or [],
        }
        raw_scope = (
            " AND JSONExtractString(payload,'property_id') IN {ids:Array(String)}"
            if ids is not None
            else ""
        )
        entity_scope = " AND property_id IN {ids:Array(String)}" if ids is not None else ""
        raw_key = "(business_key={key:String} OR (trimBoth(business_key)='' AND row_number IN {rows:Array(UInt32)}))"
        result = await client.query(
            "SELECT row_number,payload,original_text FROM raw_rows WHERE run_id={run:UUID} AND source={source:String} AND "
            + raw_key
            + raw_scope
            + " ORDER BY row_number",
            parameters=params,
        )
        raw = [
            {"row_number": n, "values": json.loads(payload), "original_text": original}
            for n, payload, original in result.result_rows
        ]
        result = await client.query(
            "SELECT payload,source_rows,type_status,reference_status FROM canonical_records WHERE run_id={run:UUID} AND source={source:String} AND business_key={key:String}"
            + entity_scope,
            parameters=params,
        )
        canonical = [
            {
                "values": json.loads(payload),
                "source_rows": rows,
                "type_status": types,
                "reference_status": refs,
            }
            for payload, rows, types, refs in result.result_rows
        ]
        result = await client.query(
            "SELECT code,description,action,impact,inclusion,row_number,actual_values,recommendation FROM quality_issues WHERE run_id={run:UUID} AND source={source:String} AND business_key={key:String}"
            + entity_scope,
            parameters=params,
        )
        issues = [
            {
                "code": code,
                "description": description,
                "action": action,
                "impact": impact,
                "inclusion": inclusion,
                "row_number": row,
                "actual_values": json.loads(values),
                "recommendation": recommendation,
            }
            for code, description, action, impact, inclusion, row, values, recommendation in result.result_rows
        ]
        return {"raw": raw, "canonical": canonical, "issues": issues}

    async def rules(self, run_id: UUID, property_id: str) -> list[dict[str, Any]]:
        """Explain commission selection using only the authorized property's source rules."""
        client = await self.helper.get_client()
        result = await client.query(
            "SELECT business_key,payload,source_rows,type_status FROM canonical_records WHERE run_id={run:UUID} AND source='commission_rules.csv' AND property_id={property:String}",
            parameters={"run": run_id, "property": property_id},
        )
        return [
            {
                "business_key": key,
                "values": json.loads(payload),
                "source_rows": rows,
                "type_status": status,
            }
            for key, payload, rows, status in result.result_rows
        ]
