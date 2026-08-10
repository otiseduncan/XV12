from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from .conftest import login


@pytest.mark.authorization
def test_admin_creates_email_free_one_time_invitation_and_user_claims_it(monkeypatch, app, client):
    monkeypatch.setenv("XV12_PUBLIC_ONBOARDING_BASE_URL", "https://bootstrap.example.test:8443")
    monkeypatch.setenv("XV12_PRIVATE_BASE_URL", "https://xoduz.example.test")
    login(client, "admin")
    created = client.post(
        "/api/admin/capabilities/invitations",
        json={
            "expires_hours": 24,
            "tailscale_invite_url": "https://login.tailscale.com/uinv/test-invite",
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["default_access"] == "chat-only"
    assert payload["setup_url"].startswith("https://bootstrap.example.test:8443/join/")
    from app.onboarding_store import OnboardingStore
    onboarding_store = OnboardingStore(app.state.permission_store.path)
    assert payload["token"] not in str(onboarding_store.list_invitations())

    qr = client.get(payload["qr_url"])
    assert qr.status_code == 200
    assert qr.headers["content-type"].startswith("image/svg+xml")

    with TestClient(app) as user_client:
        user = login(user_client, "user-a")
        claimed = user_client.post(
            "/api/admin/capabilities/invitations/claim",
            json={"token": payload["token"]},
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["user"]["id"] == user["id"]
        assert claimed.json()["grants"] == {}

    with TestClient(app) as other_client:
        login(other_client, "user-b")
        replay = other_client.post(
            "/api/admin/capabilities/invitations/claim",
            json={"token": payload["token"]},
        )
        assert replay.status_code == 409


@pytest.mark.authorization
def test_normal_user_cannot_create_or_list_invitations(client):
    login(client, "user-a")
    assert client.get("/api/admin/capabilities/invitations").status_code == 403
    response = client.post(
        "/api/admin/capabilities/invitations",
        json={"tailscale_invite_url": "https://login.tailscale.com/uinv/test-invite"},
    )
    assert response.status_code == 403

@pytest.mark.authorization
def test_tailscale_invite_api_request_omits_email(monkeypatch, client):
    import app.onboarding as onboarding_module

    seen = {}

    class FakeResponse:
        status_code = 200
        def json(self):
            return [{"id": "uinv-1", "role": "member", "inviteUrl": "https://login.tailscale.com/uinv/generated"}]

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def post(self, url, **kwargs):
            seen["url"] = url
            seen["json"] = kwargs.get("json")
            seen["authorization"] = kwargs.get("headers", {}).get("Authorization")
            return FakeResponse()

    monkeypatch.setattr(onboarding_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("XV12_TAILSCALE_API_TOKEN", "tskey-api-test")
    monkeypatch.setenv("XV12_TAILSCALE_TAILNET", "-")
    login(client, "admin")
    response = client.post("/api/admin/capabilities/invitations", json={"expires_hours": 24})
    assert response.status_code == 201, response.text
    assert seen["json"] == [{"role": "member"}]
    assert "email" not in seen["json"][0]
    assert seen["authorization"] == "Bearer tskey-api-test"
    assert seen["url"].endswith("/tailnet/-/user-invites")
