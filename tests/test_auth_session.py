from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

import app.auth as auth_module
from app.auth import GOOGLE_JWT_CLOCK_SKEW_SECONDS, verify_google_id_token
from app.main import create_app
from .conftest import login, make_settings


def _signed_google_token(settings, *, iat_offset_seconds: int = 0, nonce: str = "single-use-nonce"):
    """Build a correctly signed RS256 Google-style ID token and matching JWKS key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "google-test-key"
    now = datetime.now(UTC)
    claims = {
        "iss": "https://accounts.google.com",
        "aud": settings.google_client_id,
        "sub": "google-user-123",
        "email": "verified@example.test",
        "email_verified": True,
        "name": "Verified User",
        "nonce": nonce,
        "iat": int(now.timestamp()) + iat_offset_seconds,
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": jwk["kid"]})
    return token, jwk


def _patch_jwks(monkeypatch, jwk):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"keys": [jwk]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            assert url == "https://www.googleapis.com/oauth2/v3/certs"
            return FakeResponse()

    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeClient)


@pytest.mark.auth
def test_controlled_test_provider_maps_google_sub_not_email(client):
    admin = login(client, "admin")
    assert admin["role"] == "admin"
    client.post("/api/auth/logout")
    user = login(client, "user-a")
    assert user["role"] == "user"
    assert user["id"] != admin["id"]


@pytest.mark.auth
def test_production_google_start_has_state_nonce_and_correct_redirect(tmp_path):
    application = create_app(make_settings(tmp_path, auth_mode="google"))
    with TestClient(application) as client:
        response = client.get("/api/auth/google/start", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=" in location and "nonce=" in location
    assert "scope=openid+email+profile" in location
    state = parse_qs(urlparse(location).query)["state"][0]
    assert application.state.store.consume_oidc_attempt(state)
    assert application.state.store.consume_oidc_attempt(state) is None


@pytest.mark.auth
def test_google_id_token_cryptography_issuer_audience_and_nonce(monkeypatch, tmp_path):
    settings = make_settings(tmp_path, auth_mode="google")
    token, jwk = _signed_google_token(settings)
    _patch_jwks(monkeypatch, jwk)
    verified = asyncio.run(verify_google_id_token(token, "single-use-nonce", settings))
    assert verified["sub"] == "google-user-123"
    with pytest.raises(HTTPException, match="nonce"):
        asyncio.run(verify_google_id_token(token, "replayed-nonce", settings))


@pytest.mark.auth
def test_google_id_token_accepts_one_second_future_iat_within_leeway(monkeypatch, tmp_path):
    """Reproduce the live ImmatureSignatureError boundary and prove the bounded leeway."""
    settings = make_settings(tmp_path, auth_mode="google")
    # Live failure: iat was one second ahead of the validation instant.
    token, jwk = _signed_google_token(settings, iat_offset_seconds=1)
    _patch_jwks(monkeypatch, jwk)
    verified = asyncio.run(verify_google_id_token(token, "single-use-nonce", settings))
    assert verified["sub"] == "google-user-123"
    assert GOOGLE_JWT_CLOCK_SKEW_SECONDS == 5


@pytest.mark.auth
def test_google_id_token_rejects_materially_future_iat(monkeypatch, tmp_path):
    """A token whose iat is far beyond the leeway must still fail closed."""
    settings = make_settings(tmp_path, auth_mode="google")
    token, jwk = _signed_google_token(settings, iat_offset_seconds=30)
    _patch_jwks(monkeypatch, jwk)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(verify_google_id_token(token, "single-use-nonce", settings))
    assert exc_info.value.status_code == 401
    assert "Google identity token validation failed" in str(exc_info.value.detail)


@pytest.mark.authorization
def test_database_and_code_enforce_exactly_one_admin(client, app):
    login(client, "admin")
    client.post("/api/auth/logout")
    normal = login(client, "user-a")
    assert app.state.store.admin_count() == 1
    with app.state.store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE users SET role='admin' WHERE id=?", (normal["id"],))
    assert app.state.store.admin_count() == 1


@pytest.mark.session
def test_logout_immediately_revokes_server_session(client):
    login(client)
    assert client.get("/api/auth/me").status_code == 200
    response = client.post("/api/auth/logout")
    assert response.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.session
def test_admin_can_revoke_another_users_sessions(app):
    with TestClient(app) as admin_client, TestClient(app) as user_client:
        admin = login(admin_client, "admin")
        user = login(user_client, "user-a")
        assert user_client.get("/api/auth/me").status_code == 200
        assert admin_client.post(f"/api/admin/users/{user['id']}/revoke").status_code == 204
        assert user_client.get("/api/auth/me").status_code == 401
        assert admin_client.get("/api/auth/me").json()["id"] == admin["id"]
