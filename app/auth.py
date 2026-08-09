from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel

from .config import Settings
from .database import UserScopedStore


SESSION_COOKIE = "xv12_session"


class TestLoginRequest(BaseModel):
    persona: str


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "status": user["status"],
    }


def set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def current_user(request: Request) -> dict[str, Any]:
    store: UserScopedStore = request.app.state.store
    user = store.get_session_user(request.cookies.get(SESSION_COOKIE, ""))
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


async def verify_google_id_token(id_token: str, nonce: str, settings: Settings) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get("https://www.googleapis.com/oauth2/v3/certs")
        response.raise_for_status()
        jwks = response.json()
    header = jwt.get_unverified_header(id_token)
    key_data = next((key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid")), None)
    if not key_data:
        raise HTTPException(status_code=401, detail="Google signing key was not found")
    key = RSAAlgorithm.from_jwk(json.dumps(key_data))
    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=["RS256"],
            audience=settings.google_client_id,
            issuer=["https://accounts.google.com", "accounts.google.com"],
            options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]},
        )
    except jwt.PyJWTError as error:
        raise HTTPException(status_code=401, detail="Google identity token validation failed") from error
    if claims.get("nonce") != nonce:
        raise HTTPException(status_code=401, detail="OIDC nonce validation failed")
    if not claims.get("email_verified"):
        raise HTTPException(status_code=403, detail="A verified Google email is required")
    return claims


def create_auth_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["authentication"])

    @router.get("/config")
    def auth_config() -> dict[str, Any]:
        return {
            "mode": settings.auth_mode,
            "google_ready": bool(settings.google_client_id and settings.google_client_secret and settings.google_redirect_uri),
        }

    @router.get("/me")
    def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return public_user(user)

    @router.post("/test-login")
    def test_login(payload: TestLoginRequest, request: Request, response: Response) -> dict[str, Any]:
        if settings.auth_mode != "test":
            raise HTTPException(status_code=404, detail="Test identity provider is disabled")
        personas = {
            "admin": (settings.owner_google_sub, "owner@xv12.test", "XV12 Owner"),
            "user-a": ("test-user-a-sub", "user-a@xv12.test", "Test User A"),
            "user-b": ("test-user-b-sub", "user-b@xv12.test", "Test User B"),
        }
        if payload.persona not in personas:
            raise HTTPException(status_code=400, detail="Unknown controlled test identity")
        sub, email, name = personas[payload.persona]
        store: UserScopedStore = request.app.state.store
        user = store.upsert_oidc_user(google_sub=sub, email=email, email_verified=True, display_name=name)
        token = store.create_session(user["id"], settings.session_ttl_seconds)
        set_session_cookie(response, settings, token)
        return public_user(user)

    @router.get("/google/start")
    def google_start(request: Request) -> RedirectResponse:
        if settings.auth_mode != "google":
            raise HTTPException(status_code=404, detail="Google sign-in is not the active identity provider")
        if not (settings.google_client_id and settings.google_client_secret and settings.google_redirect_uri):
            raise HTTPException(status_code=503, detail="Google OIDC is not configured")
        store: UserScopedStore = request.app.state.store
        state, nonce = store.create_oidc_attempt()
        query = urlencode(
            {
                "client_id": settings.google_client_id,
                "redirect_uri": settings.google_redirect_uri,
                "response_type": "code",
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "prompt": "select_account",
            }
        )
        return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}", status_code=302)

    @router.get("/google/callback")
    async def google_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
        if settings.auth_mode != "google":
            raise HTTPException(status_code=404, detail="Google sign-in is not active")
        if error or not code or not state:
            raise HTTPException(status_code=401, detail="Google sign-in was not completed")
        store: UserScopedStore = request.app.state.store
        nonce = store.consume_oidc_attempt(state)
        if not nonce:
            raise HTTPException(status_code=401, detail="OIDC state is invalid, expired, or already used")
        async with httpx.AsyncClient(timeout=20) as client:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        if token_response.status_code != 200:
            raise HTTPException(status_code=401, detail="Google authorization code exchange failed")
        claims = await verify_google_id_token(token_response.json().get("id_token", ""), nonce, settings)
        user = store.upsert_oidc_user(
            google_sub=claims["sub"],
            email=claims.get("email", ""),
            email_verified=bool(claims.get("email_verified")),
            display_name=claims.get("name") or claims.get("email") or "Google user",
        )
        token = store.create_session(user["id"], settings.session_ttl_seconds)
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(response, settings, token)
        return response

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            request.app.state.store.revoke_session(token)
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=settings.cookie_secure, samesite="lax")
        response.status_code = 204
        return response

    return router
