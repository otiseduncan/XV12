from __future__ import annotations

import html
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .config import Settings
from .onboarding_store import OnboardingStore


def _capability_data_path(settings: Settings) -> Path:
    if settings.root in settings.database_path.resolve().parents:
        return settings.root / "data" / "capabilities"
    return settings.database_path.parent / "capabilities"


def _page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#071014">
  <meta name="referrer" content="no-referrer">
  <title>{html.escape(title)}</title>
  <style>
    :root{{color-scheme:dark;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#071014;color:#e8f3f7}}
    *{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(circle at top,#12303a 0,#071014 52%)}}
    main{{width:min(540px,100%);background:#0d1b21;border:1px solid #28404a;border-radius:24px;padding:28px;box-shadow:0 24px 80px #0008}}
    .mark{{width:88px;height:88px;border-radius:22px;display:grid;place-items:center;background:#102832;margin-bottom:20px;overflow:hidden}}
    .mark img{{width:100%;height:100%;object-fit:cover}}.eyebrow{{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:#8fb4c1;margin:0 0 8px}}
    h1{{font-size:30px;margin:0 0 12px}}p{{line-height:1.55;color:#b9cbd2}}.steps{{display:grid;gap:12px;margin-top:24px}}
    a.button{{display:block;text-align:center;text-decoration:none;border-radius:14px;padding:15px 18px;font-weight:750;background:#dff8ff;color:#06212a}}
    a.button.secondary{{background:#17323d;color:#e8f3f7;border:1px solid #35515c}}.step{{border:1px solid #263d46;border-radius:16px;padding:16px;background:#09171c}}
    .step b{{display:block;margin-bottom:5px}}.note{{font-size:13px;color:#8fa6af}}.ok{{color:#9ee8c1}}.warn{{color:#ffd89b}}
  </style>
</head>
<body><main>{body}</main></body></html>"""
    return HTMLResponse(
        document,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        },
    )


def create_bootstrap_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    onboarding_store = OnboardingStore(_capability_data_path(settings) / "permissions.sqlite")
    onboarding_store.initialize()

    app = FastAPI(title="XODUZ onboarding bootstrap", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.onboarding_store = onboarding_store

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "service": "xoduz-onboarding-bootstrap"}, headers={"Cache-Control": "no-store"})

    @app.get("/join/{token}")
    def join(token: str) -> HTMLResponse:
        invitation = onboarding_store.invitation_for_token(token, include_network_link=True)
        if not invitation:
            return _page(
                "Xoduz invitation unavailable",
                '<p class="eyebrow">XODUZ SETUP</p><h1>Invitation not found</h1><p>This setup link is invalid or no longer exists.</p>',
                status_code=404,
            )
        if invitation["status"] != "pending":
            label = html.escape(str(invitation["status"]).replace("_", " ").title())
            return _page(
                "Xoduz invitation unavailable",
                f'<p class="eyebrow">XODUZ SETUP</p><h1>{label}</h1><p>This one-time Xoduz setup invitation can no longer be used.</p>',
                status_code=410,
            )

        tailscale_url = str(invitation.get("tailscale_invite_url") or "")
        private_base = os.getenv("XV12_PRIVATE_BASE_URL", "").strip().rstrip("/")
        private_url = f"{private_base}/?onboard={html.escape(token, quote=True)}" if private_base else ""
        tailscale_button = (
            f'<a class="button" href="{html.escape(tailscale_url, quote=True)}" rel="noreferrer">2. Join the private Xoduz network</a>'
            if tailscale_url.startswith("https://login.tailscale.com/")
            else '<p class="warn">The Tailscale invitation is not ready. Ask the Xoduz administrator to issue a new setup QR.</p>'
        )
        xoduz_button = (
            f'<a class="button" href="{private_url}" rel="noreferrer">3. Continue to Xoduz</a>'
            if private_url
            else '<p class="warn">The private Xoduz address is not configured yet.</p>'
        )
        body = f"""
          <div class="mark"><img src="/icon" alt="Xoduz"></div>
          <p class="eyebrow">XODUZ · PRIVATE SETUP</p>
          <h1>Set up Xoduz on this phone</h1>
          <p>This one-time setup does not require the administrator to know your email. Tailscale establishes private network access; Google verifies your identity when you enter Xoduz.</p>
          <div class="steps">
            <div class="step"><b>1. Install Tailscale</b><span class="note">If Tailscale is already installed, continue to step 2.</span></div>
            <a class="button secondary" href="https://tailscale.com/download" rel="noreferrer">1. Install or open Tailscale</a>
            <div class="step"><b>2. Join the private network</b><span class="note">Accept the one-time Tailscale invitation and connect the app.</span></div>
            {tailscale_button}
            <div class="step"><b>3. Open Xoduz</b><span class="note">After Tailscale shows Connected, return to this page and continue. Xoduz will ask you to sign in with Google.</span></div>
            {xoduz_button}
            <div class="step"><b>4. Install the Xoduz app</b><span class="note">After Google sign-in, Xoduz will offer the installed-app experience and application icon.</span></div>
          </div>
          <p class="note">Invitation expires {html.escape(str(invitation['expires_at']))}. Do not forward this setup link.</p>
        """
        return _page("Set up Xoduz", body)

    @app.get("/icon")
    def icon():
        icon_path = settings.root / "assets" / "avatar" / "xoduz-icon.svg"
        if not icon_path.exists():
            raise HTTPException(status_code=404)
        return Response(icon_path.read_text(encoding="utf-8"), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    return app


app = create_bootstrap_app()
