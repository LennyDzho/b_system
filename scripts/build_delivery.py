"""Build and verify an explicit source delivery without copying local state or secrets."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "finance-dashboard"
MANIFEST = "DELIVERY_MANIFEST.json"
ROOT_FILES = (
    "README.md",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "Dockerfile",
    "compose.yaml",
    "alembic.ini",
    ".env.example",
    ".dockerignore",
    ".gitignore",
    "AGENTS.md",
    "EPIC_FINANCE_DASHBOARD.md",
)
DATA_FILES = (
    "properties.csv",
    "reservations.csv",
    "expenses.csv",
    "payments.csv",
    "commission_rules.csv",
    "README_DATA.txt",
    "Technical_Challenge_Business_Systems_Engineer.docx",
)
EXCLUDED = {".git", ".venv", "__pycache__", ".cache", ".pytest_cache", ".ruff_cache", "dist"}
SUFFIXES = {".py", ".sql", ".html", ".css", ".js", ".mako", ".md", ".txt"}


def digest(content: bytes) -> str:
    """Return a SHA-256 identity over exact source or archive bytes."""
    return hashlib.sha256(content).hexdigest()


def permitted_name(name: str) -> bool:
    """Reject traversal, local credentials, environments, caches and framework archives."""
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
        and ":" not in name
        and not set(path.parts) & EXCLUDED
        and all(part != ".env" and not part.startswith(".env.") for part in path.parts)
        and path.suffix.lower() not in {".rar", ".log", ".pyc", ".key", ".pem"}
    ) or name == ".env.example"


def source_files(root: Path) -> dict[str, Path]:
    """Collect mandatory inputs and source trees independently of Git tracking/ignore rules."""
    root = root.resolve()
    names = [*ROOT_FILES, *("data/" + name for name in DATA_FILES)]
    names.extend(["docs/codex/PROGRESS.md", *(f"docs/codex/tasks/E{i:02}.md" for i in range(1, 8))])
    for directory in ("src", "web", "tests", "scripts"):
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_file() and path.suffix in SUFFIXES and permitted_name(relative):
                names.append(relative)
    result = {}
    for name in sorted(set(names)):
        path = root / name
        if not permitted_name(name) or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Unsafe delivery path: {name}")
        if not path.is_file():
            raise ValueError(f"Missing mandatory delivery file: {name}")
        result[name] = path
    return result


def audit_content(name: str, content: bytes) -> None:
    """Reject recognizable private keys or live token formats without printing their value."""
    if name.endswith(".docx"):
        return
    patterns = (
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        rb"gh[pousr]_[A-Za-z0-9]{30,}",
        rb"github_pat_[A-Za-z0-9_]{50,}",
        rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}",
    )
    if any(re.search(pattern, content) for pattern in patterns):
        raise ValueError(f"Potential private credential in delivery file: {name}")


def verify_archive(path: Path) -> dict[str, Any]:
    """Require a complete manifest, safe names and matching hashes for every archive member."""
    with ZipFile(path) as archive:
        members = archive.namelist()
        if len(members) != len(set(members)):
            raise ValueError("Duplicate archive member")
        names = []
        for name in members:
            prefix, separator, relative = name.partition("/")
            if prefix != PREFIX or not separator or not permitted_name(relative):
                raise ValueError("Unsafe archive member")
            names.append(relative)
        manifest = json.loads(archive.read(f"{PREFIX}/{MANIFEST}"))
        expected = {record["path"]: record for record in manifest["files"]}
        if set(expected) != set(names) - {MANIFEST}:
            raise ValueError("Archive membership does not match manifest")
        for name, record in expected.items():
            content = archive.read(f"{PREFIX}/{name}")
            if len(content) != record["size"] or digest(content) != record["sha256"]:
                raise ValueError(f"Archive hash mismatch: {name}")
            audit_content(name, content)
        return {"files": len(expected), "archive_sha256": digest(path.read_bytes())}


def build(root: Path, output: Path) -> dict[str, Any]:
    """Capture exact allowed bytes, verify a staged ZIP, then atomically replace the artifact."""
    captured = {name: path.read_bytes() for name, path in source_files(root).items()}
    for name, content in captured.items():
        audit_content(name, content)
    manifest = {
        "format": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "files": [
            {"path": name, "size": len(content), "sha256": digest(content)}
            for name, content in captured.items()
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, suffix=".zip", delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
            for name, content in captured.items():
                archive.writestr(f"{PREFIX}/{name}", content)
            archive.writestr(
                f"{PREFIX}/{MANIFEST}", json.dumps(manifest, ensure_ascii=False, indent=2)
            )
        result = verify_archive(staged)
        staged.replace(output)
        output.with_suffix(output.suffix + ".sha256").write_text(
            f"{result['archive_sha256']}  {output.name}\n", encoding="utf-8"
        )
        return result
    finally:
        staged.unlink(missing_ok=True)


def main() -> None:
    """Build the local delivery or verify a supplied ZIP without publishing anything."""
    parser = argparse.ArgumentParser(description="Build or verify the local source delivery")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "finance-dashboard-e07.zip")
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    result = verify_archive(args.verify) if args.verify else build(ROOT, args.output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
