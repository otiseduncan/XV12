from __future__ import annotations

import base64
import io
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import current_user
from .config import Settings
from .enrollment import EnrollmentDenied, EnrollmentStore, ONBOARDING_COOKIE


class InitialGrant(BaseModel):
    family: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list, max_length=20)


class InvitationCreate(BaseModel):
    expires_hours: int | None = Field(default=None, ge=1, le=168)
    approval_required: bool | None = None
    initial_grants: list[InitialGrant] = Field(default_factory=list, max_length=200)
    target_user_id: str | None = None


def require_owner(request: Request, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin" or user["google_sub"] != request.app.state.settings.owner_google_sub:
        raise HTTPException(status_code=403, detail="Sole Owner access is required")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin", "").rstrip("/")
        allowed = {
            f"http://127.0.0.1:{request.app.state.settings.app_port}",
            f"http://localhost:{request.app.state.settings.app_port}",
            request.app.state.settings.tailscale_serve_origin,
            request.app.state.settings.onboarding_base_url,
        } - {""}
        if origin not in allowed or request.headers.get("x-xv12-csrf") != "1":
            raise HTTPException(status_code=403, detail="Same-origin CSRF proof is required")
    return user


class TailscaleInvites:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.tailscale_api_token and self.settings.tailscale_tailnet)

    async def create(self) -> dict[str, str]:
        if not self.configured:
            return {"status": "not_configured", "error": "Tailscale API credentials are not configured"}
        role = self.settings.tailscale_role or "member"
        if role not in {"member", "admin", "it-admin", "network-admin", "auditor", "billing-admin"}:
            return {"status": "failed", "error": "XV12_TAILSCALE_ROLE is invalid"}
        url = f"https://api.tailscale.com/api/v2/tailnet/{quote(self.settings.tailscale_tailnet, safe='')}/user-invites"
        try:
            async with httpx.AsyncClient(timeout=20, auth=(self.settings.tailscale_api_token, "")) as client:
                response = await client.post(url, json={"role": role})
            if response.status_code < 200 or response.status_code >= 300:
                response.raise_for_status()
            body = response.json()
            invite_id = str(body.get("id") or body.get("userInviteId") or "")
            invite_url = str(body.get("inviteUrl") or body.get("inviteURL") or body.get("url") or "")
            if not invite_id or not invite_url:
                return {"status": "failed", "error": "Tailscale returned an incomplete invitation response"}
            return {"status": "created", "id": invite_id, "url": invite_url}
        except (httpx.HTTPError, ValueError) as error:
            return {"status": "failed", "error": f"Tailscale invitation failed: {type(error).__name__}"}

    async def revoke(self, invite_id: str) -> dict[str, str]:
        if not self.configured or not invite_id:
            return {"status": "not_applicable"}
        url = f"https://api.tailscale.com/api/v2/user-invites/{quote(invite_id, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=20, auth=(self.settings.tailscale_api_token, "")) as client:
                response = await client.delete(url)
            if response.status_code in {200, 204, 404}:
                return {"status": "revoked" if response.status_code != 404 else "already_unavailable"}
            if response.status_code < 200 or response.status_code >= 300:
                response.raise_for_status()
        except httpx.HTTPError as error:
            return {"status": "failed", "error": f"Tailscale revocation failed: {type(error).__name__}"}
        return {"status": "revoked"}


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} · XODUZ</title><meta name='theme-color' content='#080b12'>"
        "<style>body{margin:0;background:#080b12;color:#edf3ff;font:16px/1.5 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}"
        "main{width:min(620px,calc(100% - 36px));padding:28px;border:1px solid #26334a;border-radius:22px;background:#111722;box-shadow:0 24px 70px #0008}"
        "img.logo{width:74px;height:74px;border-radius:18px}h1{margin:.5rem 0}ol{padding-left:1.3rem}.button{display:inline-block;background:#6b7cff;color:white;text-decoration:none;padding:.8rem 1rem;border-radius:12px;font-weight:700}"
        ".muted{color:#a9b4c8}.notice{padding:12px;border-radius:12px;background:#182134;border:1px solid #2b3b5b}</style></head>"
        f"<body><main><img class='logo' src='/static/icons/xoduz-192.png' alt='XODUZ'><h1>{escape(title)}</h1>{body}</main></body></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _validate_grants(request: Request, grants: list[InitialGrant]) -> list[dict[str, Any]]:
    catalog = {item["family"]: set(item["allowed_scopes"]) for item in request.app.state.registry.permission_catalog("user")}
    result: list[dict[str, Any]] = []
    for grant in grants:
        scopes = set(grant.scopes)
        if grant.family not in catalog or not scopes <= catalog[grant.family]:
            raise HTTPException(status_code=400, detail=f"Grant exceeds the user policy for {grant.family}")
        if scopes:
            result.append({"family": grant.family, "scopes": sorted(scopes)})
    return result


def _qr_data_url(value: str) -> str:
    image = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage, border=3)
    stream = io.BytesIO()
    image.save(stream)
    return "data:image/svg+xml;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


def create_remote_access_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["private-onboarding"])
    tailscale = TailscaleInvites(settings)

    @router.get("/onboard/{token}", include_in_schema=False)
    def open_invitation(token: str, request: Request) -> RedirectResponse:
        store: EnrollmentStore = request.app.state.store
        try:
            handle, _ = store.create_handoff(token)
        except EnrollmentDenied as error:
            raise HTTPException(status_code=410, detail=str(error)) from error
        response = RedirectResponse("/onboarding/connect", status_code=303)
        response.set_cookie(
            ONBOARDING_COOKIE,
            handle,
            max_age=20 * 60,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @router.get("/onboarding/connect", include_in_schema=False)
    def onboarding_connect(request: Request) -> HTMLResponse:
        invitation = request.app.state.store.invitation_for_handoff(request.cookies.get(ONBOARDING_COOKIE))
        if not invitation:
            return _page("Invitation unavailable", "<p>This invitation is invalid, expired, revoked, or already used.</p>")
        if invitation.get("tailscale_invite_url"):
            network = f"<p><a class='button' rel='noreferrer' href='{escape(invitation['tailscale_invite_url'], quote=True)}'>1. Join the private Tailscale network</a></p>"
        elif invitation.get("tailscale_status") == "failed":
            network = "<p class='notice'>The Tailscale invitation could not be created. Ask the owner to add this device to the private tailnet before continuing.</p>"
        else:
            network = "<p class='notice'>Tailscale invitation automation is not configured. Connect this device to the owner’s private tailnet before continuing.</p>"
        approval = "Owner approval will be required after Google verification." if invitation["approval_required"] else "Access activates immediately after Google verification."
        return _page(
            "Connect to XODUZ",
            f"<p>This one-time invitation enrolls one Google identity. Tailscale provides private reachability; Google verifies who you are.</p>{network}"
            f"<ol><li>Install and connect Tailscale.</li><li>Return here through the private XODUZ URL.</li><li>Verify your Google identity.</li></ol>"
            f"<p class='muted'>{escape(approval)}</p><p><a class='button' href='/api/auth/google/start'>2. Continue with Google</a></p>",
        )

    @router.get("/onboarding/pending", include_in_schema=False)
    def onboarding_pending() -> HTMLResponse:
        return _page("Approval requested", "<p>Your Google identity is verified and permanently bound. The XODUZ owner must approve access before you can sign in.</p>")

    @router.get("/onboarding/error", include_in_schema=False)
    def onboarding_error() -> HTMLResponse:
        return _page("Enrollment denied", "<p>This Google identity is not an active XODUZ user and was not presented through a valid unused invitation.</p>")

    @router.get("/api/admin/onboarding")
    def admin_state(request: Request, owner: dict[str, Any] = Depends(require_owner)) -> dict[str, Any]:
        store: EnrollmentStore = request.app.state.store
        return {
            "owner_id": owner["id"],
            "users": store.list_enrollment_users(),
            "invitations": store.list_enrollment_invitations(),
            "audit": store.audit_events(50),
            "configuration": {
                "private_origin": settings.tailscale_serve_origin,
                "onboarding_origin": settings.onboarding_base_url or settings.tailscale_serve_origin,
                "tailscale_api_configured": tailscale.configured,
                "tailscale_role": settings.tailscale_role,
                "approval_default": settings.onboarding_approval_required,
                "funnel_enabled": False,
            },
        }

    @router.post("/api/admin/onboarding/invitations", status_code=201)
    async def create_invitation(payload: InvitationCreate, request: Request, owner: dict[str, Any] = Depends(require_owner)) -> dict[str, Any]:
        store: EnrollmentStore = request.app.state.store
        grants = _validate_grants(request, payload.initial_grants)
        ttl = payload.expires_hours or settings.onboarding_invite_ttl_hours
        approval = settings.onboarding_approval_required if payload.approval_required is None else payload.approval_required
        invitation, token = store.create_enrollment_invitation(
            owner["id"],
            expires_at=(datetime.now(UTC) + timedelta(hours=ttl)).isoformat(),
            approval_required=approval,
            initial_grants=grants,
            target_user_id=payload.target_user_id,
        )
        tailnet = await tailscale.create()
        store.update_tailscale_invitation(
            invitation["id"],
            status=tailnet["status"],
            invite_id=tailnet.get("id", ""),
            invite_url=tailnet.get("url", ""),
            error=tailnet.get("error", ""),
        )
        origin = settings.onboarding_base_url or settings.tailscale_serve_origin or f"http://127.0.0.1:{settings.app_port}"
        invitation_url = f"{origin}/onboard/{token}"
        return {
            "invitation": store.invitation(invitation["id"]),
            "invitation_url": invitation_url,
            "qr_image": _qr_data_url(invitation_url),
            "tailscale": tailnet,
            "secret_visible_once": True,
        }

    @router.post("/api/admin/onboarding/invitations/{invitation_id}/approve")
    def approve(invitation_id: str, request: Request, owner: dict[str, Any] = Depends(require_owner)) -> dict[str, Any]:
        result = request.app.state.store.approve_invitation(invitation_id, owner["id"])
        if not result:
            raise HTTPException(status_code=409, detail="Invitation is not awaiting approval")
        return {"status": "active", "invitation": result}

    @router.post("/api/admin/onboarding/invitations/{invitation_id}/revoke")
    async def revoke_invitation(invitation_id: str, request: Request, owner: dict[str, Any] = Depends(require_owner)) -> dict[str, Any]:
        before = request.app.state.store.invitation(invitation_id)
        result = request.app.state.store.revoke_invitation(invitation_id, owner["id"])
        if not result:
            raise HTTPException(status_code=409, detail="Invitation cannot be revoked")
        tailnet = await tailscale.revoke(str((before or {}).get("tailscale_invite_id") or ""))
        return {"status": "revoked", "invitation": result, "tailscale": tailnet}

    @router.post("/api/admin/onboarding/users/{user_id}/revoke")
    def revoke_user(user_id: str, request: Request, owner: dict[str, Any] = Depends(require_owner)) -> dict[str, Any]:
        user = request.app.state.store.revoke_enrolled_user(user_id, owner["id"])
        if not user:
            raise HTTPException(status_code=400, detail="The sole Owner cannot be revoked or the user does not exist")
        return {"status": "revoked", "user_id": user_id, "tailnet_membership_changed": False}

    return router
