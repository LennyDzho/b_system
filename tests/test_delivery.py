"""Protect delivery membership and integrity against leaking local state or damaged files."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.build_delivery import DATA_FILES, MANIFEST, PREFIX, ROOT_FILES, build, verify_archive


def delivery_tree(root: Path) -> None:
    """Create an independent miniature source tree with every mandatory delivery input."""
    names = [
        *ROOT_FILES,
        *("data/" + name for name in DATA_FILES),
        "docs/codex/PROGRESS.md",
        *(f"docs/codex/tasks/E{i:02}.md" for i in range(1, 8)),
        "src/app/__main__.py",
        "web/templates/expense.html",
        "tests/test_example.py",
        "scripts/__init__.py",
    ]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public synthetic content\n", encoding="utf-8")


def test_delivery_excludes_local_state_and_preserves_data(tmp_path: Path) -> None:
    """Ignore real local secrets/caches/RAR while keeping ignored synthetic data and docs."""
    delivery_tree(tmp_path)
    for name in (
        ".env",
        ".git/config",
        ".venv/secret.py",
        "src/__pycache__/secret.py",
        "src/debug.log",
        "data/framework.rar",
        "data/extra.env",
        "docs/private.env",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("LOCAL_PRIVATE_SENTINEL", encoding="utf-8")
    original = (tmp_path / "data/reservations.csv").read_bytes()
    output = tmp_path / "dist/delivery.zip"
    result = build(tmp_path, output)
    assert result == verify_archive(output)
    with ZipFile(output) as archive:
        contents = [archive.read(name) for name in archive.namelist()]
        assert all(b"LOCAL_PRIVATE_SENTINEL" not in content for content in contents)
        assert archive.read(f"{PREFIX}/data/reservations.csv") == original
        assert f"{PREFIX}/docs/codex/PROGRESS.md" in archive.namelist()
        assert f"{PREFIX}/web/templates/expense.html" in archive.namelist()
    assert (tmp_path / "data/reservations.csv").read_bytes() == original


def test_delivery_rejects_private_key_without_exposing_it(tmp_path: Path) -> None:
    """Fail closed for a recognizable private key in a source file rather than packaging it."""
    delivery_tree(tmp_path)
    marker = "-----BEGIN " + "PRIVATE KEY-----"
    (tmp_path / "src/credentials.py").write_text(marker, encoding="utf-8")
    with pytest.raises(ValueError, match="Potential private credential"):
        build(tmp_path, tmp_path / "dist/delivery.zip")
    assert not (tmp_path / "dist/delivery.zip").exists()


def test_delivery_verification_detects_modified_content(tmp_path: Path) -> None:
    """Reject a ZIP whose membership matches but a payload no longer matches the manifest."""
    delivery_tree(tmp_path)
    original = tmp_path / "original.zip"
    build(tmp_path, original)
    damaged = tmp_path / "damaged.zip"
    with ZipFile(original) as source, ZipFile(damaged, "w") as destination:
        for name in source.namelist():
            content = source.read(name)
            if name.endswith("/README.md"):
                content += b"changed"
            destination.writestr(name, content)
        assert json.loads(source.read(f"{PREFIX}/{MANIFEST}"))["files"]
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_archive(damaged)
