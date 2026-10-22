"""Protect scope semantics, password handling, and exact origin validation."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.api.v1.auth.schemas import CreateUserData, Identity
from src.api.v1.auth.service import password_hasher, scope_for
from src.app.__main__ import create_app
from src.core.infra.exceptions import AuthError
from src.core.settings import AuthConfig, load_settings


def test_scope_never_widens_viewer_access() -> None:
    """Separate unrestricted admin, assigned viewer, and empty viewer scopes."""
    viewer = Identity(1, "viewer", "viewer", frozenset({"V001", "V002"}))
    assert scope_for(viewer).property_ids == ("V001", "V002")
    assert scope_for(viewer, "V002").property_ids == ("V002",)
    with pytest.raises(AuthError):
        scope_for(viewer, "V003")
    assert scope_for(Identity(2, "empty", "viewer", frozenset())).property_ids == ()
    assert scope_for(Identity(3, "admin", "admin", frozenset())).property_ids is None
    with pytest.raises(AuthError):
        scope_for(Identity(4, "invalid", "other", frozenset()))


def test_password_policy_and_argon_hash() -> None:
    """Persist only a salted Argon2id hash and reject short creation passwords."""
    raw = "unit-test-password"
    hashed = password_hasher.hash(raw)
    assert hashed.startswith("$argon2id$")
    assert raw not in hashed
    assert password_hasher.verify(raw, hashed)
    assert not password_hasher.verify("incorrect-password", hashed)
    with pytest.raises(ValidationError):
        CreateUserData(username="viewer", password="short", role="viewer")


@pytest.mark.parametrize(
    "origin", [None, "null", "https://evil.example", "http://localhost:8000.evil"]
)
def test_unsafe_requests_require_exact_origin(origin: str | None) -> None:
    """Reject login and logout CSRF before attempting database authentication."""
    application = create_app(load_settings(env_file=None))
    headers = {} if origin is None else {"Origin": origin}
    with TestClient(application) as client:
        for endpoint in ("login", "logout"):
            response = client.post(f"/api/v1/auth/{endpoint}", headers=headers, json={})
            assert response.status_code == 403


def test_validation_does_not_echo_password_or_input() -> None:
    """Invalid JSON credentials produce the common sanitized error envelope."""
    config = load_settings(env_file=None)
    with TestClient(create_app(config)) as client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": config.auth.origin},
            json={"username": "!", "password": "do-not-echo-this-password"},
        )
        assert response.status_code == 422
        assert "do-not-echo" not in response.text
        assert "input" not in response.json()


def test_https_origin_enables_secure_cookies() -> None:
    """Derive cookie security from a validated configuration, not request headers."""
    assert AuthConfig(origin="https://example.com").secure_cookie
    assert not AuthConfig(origin="http://localhost:8000").secure_cookie
    for origin in ("https://user:password@example.com", "https://example.com/path"):
        with pytest.raises(ValidationError):
            AuthConfig(origin=origin)
    with pytest.raises(ValidationError):
        AuthConfig(cookie_name="different_cookie")
