from __future__ import annotations

import io
import os
from typing import Any
from urllib.parse import quote

import httpx
import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from .auth import current_user
from .onboarding_store import OnboardingStore


class InvitationCreate(BaseModel):
    expires_hours: int = Field(default=168, ge=1, le=720)
    tailscale_invite_url: str | None = Field(default=None, max_length=2000)


class InvitationClaim(BaseModel):
    token: str = Field(min_length=20, max_length=300)


async def _create_tailscale_user_invite() -> tuple[str, str]:
    token = os.getenv("XV12_TAILSCALE_API_TOKEN", "").strip()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="Tailscale invite automation is not configured. Set XV12_TAILSCALE_API_TOKEN or provide a manual invite URL.",
        )
    tailnet = os.getenv("XV12_TAILSCALE_TAILNET", "-").strip() or "-"
    endpoint = f"https://api.tailscale.com/api/v2/tailnet/{quote(tailnet, safe='')}/user-invites"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                json=[{"role": "member"}],
            )
    except httpx.HTTPError as error:
        raise HTTPException(status_code=502, detail="Tailscale invite API could not be reached") from error
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Tailscale invite API returned {response.status_code}")
    payload = response.json()
    if not isinstance(payload, list) or not payload:
        raise HTTPException(status_code=502, detail="Tailscale invite API returned no invitation")
    invite = payload[0]
    invite_id = str(invite.get("id") or "")
    invite_url = str(invite.get("inviteUrl") or invite.get("invite_url") or "")
    if not invite_id or not invite_url.startswith("https://login.tailscale.com/"):
        raise HTTPException(status_code=502, detail="Tailscale invite API response was incomplete")
    return invite_id, invite_url


async def _delete_tailscale_user_invite(invite_id: str) -> str | None:
    token = os.getenv("XV12_TAILSCALE_API_TOKEN", "").strip()
    if not invite_id or not token:
        return None
    endpoint = f"https://api.tailscale.com/api/v2/user-invites/{quote(invite_id, safe='')}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(
                endpoint,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return "Tailscale invite could not be revoked remotely; Xoduz access remains revoked."
    if response.status_code not in {200, 204, 404}:
        return f"Tailscale invite revoke returned {response.status_code}; Xoduz access remains revoked."
    return None


def _public_setup_url(token: str) -> tuple[str, bool]:
    configured = os.getenv("XV12_PUBLIC_ONBOARDING_BASE_URL", "").strip().rstrip("/")
    if configured:
        return f"{configured}/join/{quote(token, safe='')}", True
    fallback_port = int(os.getenv("XV12_ONBOARDING_PORT", "8122"))
    return f"http://127.0.0.1:{fallback_port}/join/{quote(token, safe='')}", False


def attach_onboarding_routes(router: APIRouter, permission_store: Any) -> None:
    onboarding_store = OnboardingStore(permission_store.path)
    onboarding_store.initialize()

    def require_admin(user: dict[str, Any]) -> None:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator role required")

    @router.get("/invitations/config")
    def invitation_config(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        require_admin(user)
        return {
            "tailscale_api_ready": bool(os.getenv("XV12_TAILSCALE_API_TOKEN", "").strip()),
            "public_onboarding_ready": bool(os.getenv("XV12_PUBLIC_ONBOARDING_BASE_URL", "").strip()),
            "private_xoduz_ready": bool(os.getenv("XV12_PRIVATE_BASE_URL", "").strip()),
            "onboarding_port": int(os.getenv("XV12_ONBOARDING_PORT", "8122")),
        }

    @router.get("/invitations")
    def list_invitations(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        require_admin(user)
        return {"invitations": onboarding_store.list_invitations()}

    @router.post("/invitations", status_code=201)
    async def create_invitation(
        payload: InvitationCreate,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        require_admin(user)
        manual = (payload.tailscale_invite_url or "").strip()
        if manual:
            if not manual.startswith("https://login.tailscale.com/"):
                raise HTTPException(status_code=400, detail="Manual Tailscale invite URL must use https://login.tailscale.com/")
            tailscale_invite_id, tailscale_invite_url = None, manual
        else:
            tailscale_invite_id, tailscale_invite_url = await _create_tailscale_user_invite()
        invitation, token = onboarding_store.create_invitation(
            created_by=user["id"],
            expires_hours=payload.expires_hours,
            tailscale_invite_id=tailscale_invite_id,
            tailscale_invite_url=tailscale_invite_url,
        )
        setup_url, public_ready = _public_setup_url(token)
        return {
            "invitation": invitation,
            "token": token,
            "setup_url": setup_url,
            "public_onboarding_ready": public_ready,
            "qr_url": f"/api/admin/capabilities/invitations/{invitation['id']}/qr?token={quote(token, safe='')}",
            "default_access": "chat-only",
        }

    @router.get("/invitations/{invitation_id}/qr")
    def invitation_qr(
        invitation_id: str,
        token: str,
        user: dict[str, Any] = Depends(current_user),
    ) -> Response:
        require_admin(user)
        invitation = onboarding_store.invitation_for_token(token)
        if not invitation or invitation["id"] != invitation_id or invitation["status"] != "pending":
            raise HTTPException(status_code=404, detail="Invitation is not available for QR generation")
        setup_url, _ = _public_setup_url(token)
        image = qrcode.make(setup_url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=3)
        buffer = io.BytesIO()
        image.save(buffer)
        return Response(
            content=buffer.getvalue(),
            media_type="image/svg+xml",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.post("/invitations/claim")
    def claim_invitation(
        payload: InvitationClaim,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        if user["role"] != "user":
            raise HTTPException(status_code=400, detail="Administrator accounts cannot claim user invitations")
        try:
            invitation = onboarding_store.claim_invitation(payload.token, user)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {
            "status": "active",
            "invitation": invitation,
            "user": {
                "id": user["id"],
                "display_name": user["display_name"],
                "email": user["email"],
                "role": user["role"],
            },
            "grants": permission_store.grants_for(user["id"]),
        }

    @router.delete("/invitations/{invitation_id}")
    async def revoke_invitation(
        invitation_id: str,
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        require_admin(user)
        current = onboarding_store.invitation_for_id(invitation_id, include_network_link=True)
        if not current:
            raise HTTPException(status_code=404, detail="Invitation not found")
        if current["status"] == "active":
            raise HTTPException(status_code=409, detail="Claimed invitations cannot be revoked; manage the registered user instead")
        updated = onboarding_store.revoke_invitation(invitation_id)
        warning = await _delete_tailscale_user_invite(str(current.get("tailscale_invite_id") or ""))
        return {"status": "revoked", "invitation": updated, "warning": warning}
