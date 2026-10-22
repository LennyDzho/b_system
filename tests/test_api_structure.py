"""Protect standalone CLI imports and the single HTTP adapter for core failures."""

import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from src.app.__main__ import create_app
from src.core.infra.exceptions import AuthError, DashboardError, NotFound
from src.core.settings import BASE_DIR, load_settings


def test_cli_imports_do_not_assemble_http_routes_or_web() -> None:
    """Import both real CLI entry points in a fresh interpreter without loading HTTP routes."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,sys; import src.cli.create_user,src.cli.import_data; "
            "print(json.dumps([name for name in sys.modules if "
            "(name.startswith('src.api.') and name.endswith('.routers')) or "
            "name == 'web' or name.startswith('web.')]))",
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    "exception,expected_status,expected_message",
    [
        (AuthError(403, "Public denial"), 403, "Public denial"),
        (DashboardError("Public availability failure"), 503, "Public availability failure"),
        (NotFound("private-key-value"), 404, "Запись не найдена"),
    ],
)
def test_core_failures_use_the_shared_http_handler(
    exception: Exception, expected_status: int, expected_message: str
) -> None:
    """Translate access, analytical and repository failures without leaking private context."""
    app = create_app(load_settings(env_file=None))

    async def fail() -> None:
        """Raise a core failure without opening a database connection."""
        raise exception

    app.add_api_route("/test-domain-error", fail)
    with TestClient(app) as client:
        response = client.get("/test-domain-error")
    assert response.status_code == expected_status
    assert response.json()["error"]["message"] == expected_message
    assert response.headers["cache-control"] == "no-store"
    assert "private-key-value" not in response.text
