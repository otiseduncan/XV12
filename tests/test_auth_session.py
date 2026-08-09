from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from .conftest import login, make_settings


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
