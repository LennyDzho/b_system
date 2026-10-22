"""Check exact row text and physical lineage with multiline and Unicode CSV values."""

from pathlib import Path

from src.services.imports.parser import capture
from tests.import_helpers import write_sources


def test_physical_lines_are_not_unicode_string_line_separators(tmp_path: Path) -> None:
    """Keep embedded newlines and U+0085 inside values without shifting later row text."""
    values = [
        {
            "property_id": key,
            "property_name": name,
            "cluster": "Area",
            "owner_id": "owner",
            "base_currency": "IDR",
        }
        for key, name in [("one", "Villa\nNorth\u0085Wing"), ("two", "Second")]
    ]
    write_sources(tmp_path, {"properties.csv": values})
    rows = [row for row in capture(tmp_path).raw if row.source == "properties.csv"]
    assert [row.row_number for row in rows] == [2, 4]
    assert rows[0].payload["property_name"] == "Villa\nNorth\u0085Wing"
    assert rows[1].original_text.startswith("two,Second,")
