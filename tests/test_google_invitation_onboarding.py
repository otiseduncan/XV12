from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

import app.auth as auth_module
import app.remote_access as remote_module
from app.main import create_app
from tests.conftest import FakeModel, make_settings


ORIGIN = "http://127.0.0.1:8120"
OWNER_HEADERS = {"Origin": ORIGIN, "X-XV12-CSRF": "1"}


def google_app(tmp_path, **changes):
    settings = replace(
        make_settings(tmp_path, auth_mode="google"),
        cookie_secure=False,
        tailscale_serve_origin="https://xv12-device.example.ts.net:10000",
        onboarding_base_url=ORIGIN,
        **changes,
    )
    application = create_app(settings)
    application.state.model = FakeModel()
    return application


def owner_client(application) -> TestClient:
    owner = application.state.store.upsert_oidc_user(
        google_sub=application.state.settings.owner_google_sub,
        email="owner@example.com",
        email_verified=True,
        display_name="Otis",
    )
    token = application.state.store.create_session(owner["id"], 3600)
    client = TestClient(application, base_url=ORIGIN, follow_redirects=False)
    client.cookies.set("xv12_session", token)
    return client


def create_invite(client: TestClient, **payload):
    response = client.post("/api/admin/onboarding/invitations", headers=OWNER_HEADERS, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class FakeGoogleClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return httpx.Response(200, json={"id_token": "controlled-id-token"})


def google_callback(client: TestClient, monkeypatch, claims: dict[str, object]):
    async def verified(*args, **kwargs):
        return claims

    monkeypatch.setattr(auth_module, "verify_google_id_token", verified)
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", FakeGoogleClient)
    start = client.get("/api/auth/google/start")
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return client.get(f"/api/auth/google/callback?code=controlled&state={state}")


def begin_invitation(client: TestClient, invitation_url: str):
    path = urlparse(invitation_url).path
    opened = client.get(path)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/onboarding/connect"
    assert "xv12_onboarding" in client.cookies


def test_unknown_google_identity_cannot_auto_provision(tmp_path, monkeypatch):
    app = google_app(tmp_path)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        response = google_callback(client, monkeypatch, {"sub": "unknown-sub", "email": "new@example.com", "email_verified": True, "name": "New User"})
        assert response.status_code == 303
        assert response.headers["location"] == "/onboarding/error"
        assert app.state.store.list_enrollment_users() == [next(user for user in app.state.store.list_enrollment_users() if user["role"] == "admin")]


def test_invitation_token_is_stripped_before_google_redirect(tmp_path):
    app = google_app(tmp_path)
    with owner_client(app) as owner:
        invitation = create_invite(owner)
        assert invitation["tailscale"]["status"] == "not_configured"
        assert invitation["qr_image"].startswith("data:image/svg+xml;base64,")
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        token = urlparse(invitation["invitation_url"]).path.rsplit("/", 1)[-1]
        begin_invitation(recipient, invitation["invitation_url"])
        page = recipient.get("/onboarding/connect")
        assert page.status_code == 200
        assert token not in page.text
        assert "Google verifies who you are" in page.text
        assert "Tailscale provides private reachability" in page.text


def test_invite_claim_binds_google_sub_and_requires_owner_approval(tmp_path, monkeypatch):
    app = google_app(tmp_path)
    with owner_client(app) as owner:
        invitation = create_invite(owner, approval_required=True)
        invitation_id = invitation["invitation"]["id"]
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        begin_invitation(recipient, invitation["invitation_url"])
        callback = google_callback(recipient, monkeypatch, {"sub": "immutable-google-sub", "email": "person@example.com", "email_verified": True, "name": "Person One"})
        assert callback.status_code == 303
        assert callback.headers["location"].startswith("/onboarding/pending")
        assert "xv12_session" not in recipient.cookies
    claimed = app.state.store.invitation(invitation_id)
    assert claimed["status"] == "pending_approval"
    assert claimed["claimed_google_sub"] == "immutable-google-sub"
    assert claimed["claimed_email"] == "person@example.com"
    with owner_client(app) as owner:
        approved = owner.post(f"/api/admin/onboarding/invitations/{invitation_id}/approve", headers=OWNER_HEADERS, json={})
        assert approved.status_code == 200
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        callback = google_callback(recipient, monkeypatch, {"sub": "immutable-google-sub", "email": "renamed@example.com", "email_verified": True, "name": "Person Renamed"})
        assert callback.status_code == 303 and callback.headers["location"] == "/"
        assert "xv12_session" in recipient.cookies


def test_invitation_is_atomic_one_use_for_google_identity(tmp_path, monkeypatch):
    app = google_app(tmp_path, onboarding_approval_required=False)
    with owner_client(app) as owner:
        invitation = create_invite(owner, approval_required=False)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as first:
        begin_invitation(first, invitation["invitation_url"])
        response = google_callback(first, monkeypatch, {"sub": "first-sub", "email": "first@example.com", "email_verified": True, "name": "First"})
        assert response.status_code == 303 and response.headers["location"] == "/"
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as replay:
        assert replay.get(urlparse(invitation["invitation_url"]).path).status_code == 410
        denied = google_callback(replay, monkeypatch, {"sub": "second-sub", "email": "second@example.com", "email_verified": True, "name": "Second"})
        assert denied.headers["location"] == "/onboarding/error"
    users = [user for user in app.state.store.list_enrollment_users() if user["role"] == "user"]
    assert [user["google_sub"] for user in users] == ["first-sub"]


def test_initial_capability_grants_activate_only_after_approval(tmp_path, monkeypatch):
    app = google_app(tmp_path)
    family = next(item for item in app.state.registry.permission_catalog("user") if item["allowed_scopes"])
    scope = family["allowed_scopes"][0]
    with owner_client(app) as owner:
        invitation = create_invite(owner, approval_required=True, initial_grants=[{"family": family["family"], "scopes": [scope]}])
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        begin_invitation(recipient, invitation["invitation_url"])
        google_callback(recipient, monkeypatch, {"sub": "granted-sub", "email": "granted@example.com", "email_verified": True, "name": "Granted"})
    claimed = app.state.store.invitation(invitation["invitation"]["id"])
    assert app.state.permission_store.grants_for(claimed["claimed_user_id"]) == {}
    with owner_client(app) as owner:
        owner.post(f"/api/admin/onboarding/invitations/{claimed['id']}/approve", headers=OWNER_HEADERS, json={})
    assert app.state.permission_store.grants_for(claimed["claimed_user_id"])[family["family"]] == [scope]


def test_owner_cannot_be_revoked_and_admin_writes_require_csrf(tmp_path):
    app = google_app(tmp_path)
    with owner_client(app) as owner:
        owner_id = owner.get("/api/auth/me").json()["id"]
        assert owner.post("/api/admin/onboarding/invitations", json={}).status_code == 403
        assert owner.post(f"/api/admin/onboarding/users/{owner_id}/revoke", headers=OWNER_HEADERS, json={}).status_code == 400


def test_revoked_google_identity_requires_targeted_reinvitation(tmp_path, monkeypatch):
    app = google_app(tmp_path, onboarding_approval_required=False)
    with owner_client(app) as owner:
        invitation = create_invite(owner, approval_required=False)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        begin_invitation(recipient, invitation["invitation_url"])
        google_callback(recipient, monkeypatch, {"sub": "returning-sub", "email": "returning@example.com", "email_verified": True, "name": "Returning"})
    user = next(item for item in app.state.store.list_enrollment_users() if item["google_sub"] == "returning-sub")
    with owner_client(app) as owner:
        assert owner.post(f"/api/admin/onboarding/users/{user['id']}/revoke", headers=OWNER_HEADERS, json={}).status_code == 200
        generic = create_invite(owner, approval_required=False)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        begin_invitation(recipient, generic["invitation_url"])
        denied = google_callback(recipient, monkeypatch, {"sub": "returning-sub", "email": "returning@example.com", "email_verified": True, "name": "Returning"})
        assert denied.headers["location"] == "/onboarding/error"
    with owner_client(app) as owner:
        targeted = create_invite(owner, approval_required=False, target_user_id=user["id"])
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        begin_invitation(recipient, targeted["invitation_url"])
        accepted = google_callback(recipient, monkeypatch, {"sub": "returning-sub", "email": "returning@example.com", "email_verified": True, "name": "Returning"})
        assert accepted.headers["location"] == "/"


def test_tailscale_invite_api_is_email_less_and_revocable(tmp_path, monkeypatch):
    requests = []

    class FakeTailscaleClient(FakeGoogleClient):
        async def post(self, url, json):
            requests.append(("POST", url, json))
            return httpx.Response(200, json={"id": "invite-123", "inviteUrl": "https://login.tailscale.com/admin/invite/abc"})

        async def delete(self, url):
            requests.append(("DELETE", url, None))
            return httpx.Response(204)

    monkeypatch.setattr(remote_module.httpx, "AsyncClient", FakeTailscaleClient)
    app = google_app(tmp_path, tailscale_api_token="tskey-api-test", tailscale_tailnet="example.com")
    with owner_client(app) as owner:
        invitation = create_invite(owner)
        assert invitation["tailscale"]["status"] == "created"
        assert requests[0][2] == {"role": "member"}
        invitation_id = invitation["invitation"]["id"]
        revoked = owner.post(f"/api/admin/onboarding/invitations/{invitation_id}/revoke", headers=OWNER_HEADERS, json={})
        assert revoked.json()["tailscale"]["status"] == "revoked"
        assert requests[-1][0] == "DELETE" and requests[-1][1].endswith("/user-invites/invite-123")


def test_pwa_manifest_icons_and_service_worker_are_private_data_safe(tmp_path):
    app = google_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        manifest = client.get("/static/manifest.webmanifest").json()
        assert manifest["display"] == "standalone"
        assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
        worker_response = client.get("/service-worker.js")
        assert worker_response.headers["service-worker-allowed"] == "/"
        worker = worker_response.text
        assert 'url.pathname.startsWith("/api/")' in worker
        assert 'url.pathname.startsWith("/onboard/")' in worker
        assert client.get("/static/icons/xoduz-192.png").headers["content-type"] == "image/png"
