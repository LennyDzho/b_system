"""Ensure invalid source values stay nullable and accurately attributed in real CH."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from src.core.settings import Settings
from src.services.imports.parser import capture
from src.services.imports.service import ImportService
from tests.import_helpers import write_sources

pytestmark = pytest.mark.integration


async def test_partial_snapshot_nulls_and_selected_version_provenance(
    environment: tuple[ImportService, Settings], tmp_path: Path
) -> None:
    """Publish partial data and point issues to the selected raw version, not an older one."""
    service, _ = environment
    write_sources(tmp_path)
    snapshot = capture(tmp_path)
    original = next(row.payload for row in snapshot.raw if row.source == "reservations.csv")
    latest = deepcopy(original)
    latest.update(updated_at="2026-09-02", property_id="unknown", gross_booking_value="1e1000000")
    payments = [row.payload for row in snapshot.raw if row.source == "payments.csv"]
    conflicting = deepcopy(payments[0])
    conflicting["amount"] = "200"
    write_sources(
        tmp_path, {"reservations.csv": [original, latest], "payments.csv": payments + [conflicting]}
    )
    result = await service.run(tmp_path, "test")
    assert result["summary"]["partial"] and result["calculation_version"] == "idr-v1"
    client = await service.analytics.helper.get_client()
    rows = await client.query(
        "SELECT type_status, gross_booking_value, source_rows FROM canonical_records WHERE run_id={id:UUID} AND source='reservations.csv'",
        parameters={"id": result["run_id"]},
    )
    assert rows.result_rows == [("invalid", None, [2, 3])]
    issue = await client.query(
        "SELECT row_number, actual_values FROM quality_issues WHERE run_id={id:UUID} AND source='reservations.csv' AND code='unknown_property'",
        parameters={"id": result["run_id"]},
    )
    assert issue.result_rows[0][0] == 3
    assert json.loads(issue.result_rows[0][1])["property_id"] == "unknown"
