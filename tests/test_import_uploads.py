"""Verify bounded multipart handling and closure of partial uploads without databases."""

from collections.abc import Callable
from typing import Any, BinaryIO

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request

from src.api.v1.imports import uploads
from src.app.__main__ import create_app
from src.core.infra.exceptions import WebImportError
from src.core.settings import load_settings


def upload_request(
    files: list[tuple[str, tuple[str, bytes]]],
    *,
    stream: bool = False,
    disconnect: bool = False,
    fields: dict[str, str] | None = None,
) -> tuple[Request, int]:
    """Encode real multipart bytes and optionally stream them without a length header."""
    encoded = httpx.Request("POST", "http://test/imports", files=files, data=fields)
    body = encoded.read()
    chunks = [body[i : i + 32] for i in range(0, len(body), 32)] if stream else [body]
    headers = [
        (key.lower(), value)
        for key, value in encoded.headers.raw
        if not (stream and key.lower() == b"content-length")
    ]

    async def receive() -> dict[str, Any]:
        """Return successive transport chunks or an actual disconnected request message."""
        if disconnect and len(chunks) < 3:
            return {"type": "http.disconnect"}
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    return Request(
        {"type": "http", "method": "POST", "path": "/api/v1/imports", "headers": headers}, receive
    ), len(body)


@pytest.fixture
def spool_files(monkeypatch: pytest.MonkeyPatch) -> list[BinaryIO]:
    """Track parser-owned files so failed and cancelled parsing cannot silently leak them."""
    original: Callable[..., BinaryIO] = uploads.MultiPartParser.__init__.__globals__[
        "SpooledTemporaryFile"
    ]
    observed: list[BinaryIO] = []

    def track(*args: Any, **kwargs: Any) -> BinaryIO:
        """Record each real spool file while preserving the actual parser behavior."""
        file = original(*args, **kwargs)
        observed.append(file)
        return file

    monkeypatch.setitem(uploads.MultiPartParser.__init__.__globals__, "SpooledTemporaryFile", track)
    return observed


async def test_upload_preserves_bytes_and_closes_spools(spool_files: list[BinaryIO]) -> None:
    """Leave BOM, CRLF and embedded bytes untouched for the shared canonicalizer."""
    content = b"\xef\xbb\xbfa,b\r\n1,2\r\n"
    request, _ = upload_request([("files", ("properties.csv", content))], stream=True)
    assert await uploads.read_upload(request) == {"properties.csv": content}
    assert spool_files and all(file.closed for file in spool_files)


@pytest.mark.parametrize("name", ["../properties.csv", "C:\\properties.csv", "other.csv"])
async def test_untrusted_names_are_rejected(name: str, spool_files: list[BinaryIO]) -> None:
    """Never turn an uploaded filename into a server path."""
    request, _ = upload_request([("files", (name, b"csv"))])
    with pytest.raises(WebImportError) as error:
        await uploads.read_upload(request)
    assert error.value.status_code == 422
    assert all(file.closed for file in spool_files)


@pytest.mark.parametrize("case", ["duplicate", "too_many", "actor_field"])
async def test_duplicate_extra_files_and_actor_fields_are_rejected(
    case: str, spool_files: list[BinaryIO]
) -> None:
    """Forbid ambiguous file sets and caller-supplied audit identity."""
    files = [("files", ("properties.csv", b"csv"))] * (
        6 if case == "too_many" else 2 if case == "duplicate" else 1
    )
    request, _ = upload_request(
        files, fields={"actor": "forged"} if case == "actor_field" else None
    )
    with pytest.raises(WebImportError) as error:
        await uploads.read_upload(request)
    assert error.value.status_code == 422
    assert all(file.closed for file in spool_files)


async def test_streamed_body_limit_ignores_absent_length(
    monkeypatch: pytest.MonkeyPatch, spool_files: list[BinaryIO]
) -> None:
    """Reject a too-large streamed body and close a file received before the limit."""
    request, size = upload_request([("files", ("properties.csv", b"a" * 1024))], stream=True)
    monkeypatch.setattr(uploads, "MAX_BODY_BYTES", size - 40)
    with pytest.raises(WebImportError) as error:
        await uploads.read_upload(request)
    assert error.value.status_code == 413
    assert spool_files and all(file.closed for file in spool_files)


async def test_actual_file_size_is_limited(
    monkeypatch: pytest.MonkeyPatch, spool_files: list[BinaryIO]
) -> None:
    """Do not confuse Starlette's field-size limit with a limit on uploaded files."""
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 10)
    request, _ = upload_request([("files", ("properties.csv", b"a" * 11))])
    with pytest.raises(WebImportError) as error:
        await uploads.read_upload(request)
    assert error.value.status_code == 413
    assert all(file.closed for file in spool_files)


async def test_disconnected_body_closes_partial_file(spool_files: list[BinaryIO]) -> None:
    """Close spool files even when disconnect is not a multipart-parser exception."""
    request, _ = upload_request(
        [("files", ("properties.csv", b"a" * 1024))], stream=True, disconnect=True
    )
    with pytest.raises(ClientDisconnect):
        await uploads.read_upload(request)
    assert spool_files and all(file.closed for file in spool_files)


def test_auth_database_outage_matches_import_error_schema() -> None:
    """Use a real refused PG TCP connection; report unavailable before parsing the body."""
    config = load_settings(env_file=None)
    config.db.host, config.db.port, config.db.timeout = "127.0.0.1", 1, 0.2
    with TestClient(create_app(config)) as client:
        client.cookies.set(config.auth.cookie_name, "upload-contract-session")
        response = client.post(
            "/api/v1/imports", content=b"not multipart", headers={"Origin": config.auth.origin}
        )
    assert response.status_code == 503
    assert response.json()["outcome"] == "unavailable"
    assert response.json()["run_id"] is None
    assert response.headers["cache-control"] == "no-store"
