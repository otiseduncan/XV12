from __future__ import annotations

import io
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import app.auth as auth_module
import app.remote_access as remote_module
from app.database import UserScopedStore
from app.enrollment import EnrollmentDenied, OwnerBootstrapDenied, _oidc_invitation_id
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


def test_one_time_owner_bootstrap_binds_verified_google_sub_and_disables_itself(tmp_path, monkeypatch):
    app = google_app(tmp_path)
    private_env = tmp_path / ".env.local"
    private_env.write_text(
        "XV12_AUTH_MODE=google\nXV12_OWNER_GOOGLE_SUB=test-admin-sub\nXV12_GOOGLE_CLIENT_SECRET=preserved-secret\n",
        encoding="utf-8",
    )
    app.state.store.owner_env_path = private_env
    bootstrap_id, token = app.state.store.issue_owner_bootstrap(expires_minutes=10)

    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as owner:
        opened = owner.get(f"/owner-bootstrap/{token}")
        assert opened.status_code == 303
        assert opened.headers["location"] == "/api/auth/google/start"
        assert token not in opened.headers["location"]
        assert "xv12_owner_bootstrap" in owner.cookies
        callback = google_callback(
            owner,
            monkeypatch,
            {"sub": "verified-owner-google-sub", "email": "owner@example.com", "email_verified": True, "name": "Verified Owner"},
        )
        assert callback.status_code == 303 and callback.headers["location"] == "/"
        assert "xv12_owner_bootstrap" not in owner.cookies
        me = owner.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["role"] == "admin"

    assert app.state.settings.owner_google_sub == "verified-owner-google-sub"
    assert app.state.store.owner_google_sub == "verified-owner-google-sub"
    assert "XV12_OWNER_GOOGLE_SUB=verified-owner-google-sub" in private_env.read_text(encoding="utf-8")
    assert "XV12_GOOGLE_CLIENT_SECRET=preserved-secret" in private_env.read_text(encoding="utf-8")
    with app.state.store.connect() as db:
        bootstrap = db.execute("SELECT status FROM owner_bootstraps WHERE id=?", (bootstrap_id,)).fetchone()
        admin = db.execute("SELECT google_sub FROM users WHERE role='admin'").fetchone()
    assert bootstrap["status"] == "consumed"
    assert admin["google_sub"] == "verified-owner-google-sub"
    with pytest.raises(OwnerBootstrapDenied):
        app.state.store.issue_owner_bootstrap()
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as replay:
        assert replay.get(f"/owner-bootstrap/{token}").status_code == 410


def test_owner_bootstrap_env_write_failure_rolls_back_database(tmp_path, monkeypatch):
    app = google_app(tmp_path)
    private_env = tmp_path / ".env.local"
    original_env = b"XV12_AUTH_MODE=google\nXV12_OWNER_GOOGLE_SUB=test-admin-sub\n"
    private_env.write_bytes(original_env)
    app.state.store.owner_env_path = private_env
    bootstrap_id, _ = app.state.store.issue_owner_bootstrap(expires_minutes=10)

    def fail_env_write(*_args):
        raise OSError("simulated private env write failure")

    monkeypatch.setattr(app.state.store, "_persist_owner_sub", fail_env_write)
    with pytest.raises(OSError, match="simulated private env write failure"):
        app.state.store._claim_owner_bootstrap(
            bootstrap_id,
            google_sub="verified-owner-google-sub",
            email="owner@example.com",
            email_verified=True,
            display_name="Verified Owner",
        )

    assert private_env.read_bytes() == original_env
    with app.state.store.connect() as db:
        admin = db.execute("SELECT google_sub FROM users WHERE role='admin'").fetchone()
        bootstrap = db.execute(
            "SELECT status FROM owner_bootstraps WHERE id=?", (bootstrap_id,)
        ).fetchone()
    assert admin["google_sub"] == "test-admin-sub"
    assert bootstrap["status"] == "active"


def test_owner_bootstrap_commit_failure_restores_env_and_database(tmp_path, monkeypatch):
    app = google_app(tmp_path)
    private_env = tmp_path / ".env.local"
    original_env = b"XV12_AUTH_MODE=google\nXV12_OWNER_GOOGLE_SUB=test-admin-sub\n"
    private_env.write_bytes(original_env)
    store = app.state.store
    store.owner_env_path = private_env
    bootstrap_id, _ = store.issue_owner_bootstrap(expires_minutes=10)
    normal_connect = store.connect

    @contextmanager
    def fail_commit():
        connection = sqlite3.connect(store.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.rollback()
            raise sqlite3.OperationalError("simulated commit failure")
        finally:
            connection.close()

    monkeypatch.setattr(store, "connect", fail_commit)
    with pytest.raises(sqlite3.OperationalError, match="simulated commit failure"):
        store._claim_owner_bootstrap(
            bootstrap_id,
            google_sub="verified-owner-google-sub",
            email="owner@example.com",
            email_verified=True,
            display_name="Verified Owner",
        )
    monkeypatch.setattr(store, "connect", normal_connect)

    assert private_env.read_bytes() == original_env
    with store.connect() as db:
        admins = db.execute(
            "SELECT google_sub FROM users WHERE role='admin' AND status='active'"
        ).fetchall()
        bootstrap = db.execute(
            "SELECT status FROM owner_bootstraps WHERE id=?", (bootstrap_id,)
        ).fetchone()
    assert [row["google_sub"] for row in admins] == ["test-admin-sub"]
    assert bootstrap["status"] == "active"


def test_owner_bootstrap_rejects_identity_bound_to_another_user(tmp_path):
    app = google_app(tmp_path)
    private_env = tmp_path / ".env.local"
    private_env.write_text(
        "XV12_AUTH_MODE=google\nXV12_OWNER_GOOGLE_SUB=test-admin-sub\n",
        encoding="utf-8",
    )
    store = app.state.store
    store.owner_env_path = private_env
    UserScopedStore.upsert_oidc_user(
        store,
        google_sub="already-bound-sub",
        email="existing@example.com",
        email_verified=True,
        display_name="Existing User",
    )
    bootstrap_id, _ = store.issue_owner_bootstrap(expires_minutes=10)

    with pytest.raises(OwnerBootstrapDenied, match="already bound"):
        store._claim_owner_bootstrap(
            bootstrap_id,
            google_sub="already-bound-sub",
            email="owner@example.com",
            email_verified=True,
            display_name="Verified Owner",
        )

    with store.connect() as db:
        admins = db.execute(
            "SELECT google_sub FROM users WHERE role='admin' AND status='active'"
        ).fetchall()
        bootstrap = db.execute(
            "SELECT status FROM owner_bootstraps WHERE id=?", (bootstrap_id,)
        ).fetchone()
    assert [row["google_sub"] for row in admins] == ["test-admin-sub"]
    assert bootstrap["status"] == "active"


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


def test_concurrent_invitation_claim_allows_exactly_one_identity(tmp_path):
    app = google_app(tmp_path, onboarding_approval_required=False)
    with owner_client(app) as owner:
        invitation = create_invite(owner, approval_required=False)
    invitation_id = invitation["invitation"]["id"]
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def claim(suffix: str) -> None:
        context = _oidc_invitation_id.set(invitation_id)
        try:
            barrier.wait(timeout=5)
            user = app.state.store.upsert_oidc_user(
                google_sub=f"race-{suffix}",
                email=f"race-{suffix}@example.com",
                email_verified=True,
                display_name=f"Race {suffix}",
            )
            results.append(("accepted", user["google_sub"]))
        except EnrollmentDenied:
            results.append(("denied", suffix))
        finally:
            _oidc_invitation_id.reset(context)

    threads = [threading.Thread(target=claim, args=(suffix,)) for suffix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert [result[0] for result in results].count("accepted") == 1
    assert [result[0] for result in results].count("denied") == 1
    claimed = app.state.store.invitation(invitation_id)
    assert claimed["status"] == "active"
    assert claimed["claimed_google_sub"] == next(result[1] for result in results if result[0] == "accepted")


def test_expired_and_revoked_invitation_links_fail_closed(tmp_path):
    app = google_app(tmp_path)
    with owner_client(app) as owner:
        expired = create_invite(owner)
        revoked = create_invite(owner)
        with app.state.store.connect() as db:
            db.execute(
                "UPDATE enrollment_invitations SET expires_at=? WHERE id=?",
                ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), expired["invitation"]["id"]),
            )
        assert owner.post(
            f"/api/admin/onboarding/invitations/{revoked['invitation']['id']}/revoke",
            headers=OWNER_HEADERS,
            json={},
        ).status_code == 200
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        assert recipient.get(urlparse(expired["invitation_url"]).path).status_code == 410
        assert recipient.get(urlparse(revoked["invitation_url"]).path).status_code == 410


def test_invitation_enrolled_user_is_conversation_only_until_granted(tmp_path, monkeypatch):
    app = google_app(tmp_path, onboarding_approval_required=False)
    with owner_client(app) as owner:
        invitation = create_invite(owner, approval_required=False)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as recipient:
        begin_invitation(recipient, invitation["invitation_url"])
        accepted = google_callback(
            recipient,
            monkeypatch,
            {"sub": "bounded-sub", "email": "bounded@example.com", "email_verified": True, "name": "Bounded User"},
        )
        assert accepted.status_code == 303 and accepted.headers["location"] == "/"
        assert recipient.get("/api/onboarding/me").json() == {"invitation_enrolled": True, "conversation_only": True}
        assert recipient.post("/api/conversations", json={"title": "Allowed chat"}).status_code == 201
        assert recipient.get("/api/projects").status_code == 403
        assert recipient.post("/api/attachments", files={"file": ("blocked.txt", io.BytesIO(b"blocked"), "text/plain")}).status_code == 403
        assert recipient.get("/api/runtime/fingerprint").status_code == 403
        assert recipient.get("/api/creator/jobs/not-a-job").status_code == 403
        listing = recipient.get("/api/capabilities").json()["capabilities"]
        assert listing == []
        assert recipient.post("/api/capabilities/project.list", json={"arguments": {}}).status_code == 403
        enrolled = app.state.store.invitation(invitation["invitation"]["id"])
        with owner_client(app) as owner:
            granted = owner.put(
                f"/api/admin/capabilities/users/{enrolled['claimed_user_id']}/grants",
                json={"grants": [{"family": "projects", "scopes": ["read"]}]},
            )
            assert granted.status_code == 200
        assert recipient.post("/api/capabilities/project.list", json={"arguments": {}}).status_code == 200


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
        assert invitation["tailscale_qr_image"].startswith("data:image/svg+xml;base64,")
        assert invitation["invitation"]["tailscale_invite_url"] is None
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
        assert 'const CACHE = "xoduz-shell-v2"' in worker
        assert '"/static/app.js?v=4.1.0"' in worker
        assert 'url.pathname.startsWith("/api/")' in worker
        assert 'url.pathname.startsWith("/onboard/")' in worker
        assert client.get("/static/icons/xoduz-192.png").headers["content-type"] == "image/png"
