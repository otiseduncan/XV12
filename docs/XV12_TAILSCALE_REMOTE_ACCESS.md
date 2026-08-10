# XV12 private onboarding, Google SSO, and Android PWA

XV12 remains bound to `127.0.0.1:8120`. Tailscale Serve provides private HTTPS reachability to that loopback service. The launcher never invokes Funnel and does not expose XV12, llama-server, ComfyUI, or another local service to the public internet.

## Security boundaries

The four layers are independent and cumulative:

1. **Tailscale** limits network reachability to the private tailnet.
2. **Google OIDC** authenticates the person using the existing server-side authorization-code flow.
3. **XV12 invitations** control whether a new immutable Google `sub` may enroll.
4. **XV12 grants** control which capability families and scopes that active user may execute.

Tailscale headers are not XV12 authentication. A tailnet identity cannot create an XV12 session. Google tokens never reach JavaScript. Existing active XV12 users continue to sign in normally; an unknown Google identity requires a valid, unused invitation.

The sole Owner remains the account bound to `XV12_OWNER_GOOGLE_SUB`. Email and display name are metadata and cannot transfer ownership.

## Private configuration

Create untracked `config/.env.local` from `.env.example`. This deployment's non-secret topology is:

```dotenv
XV12_AUTH_MODE=google
XV12_GOOGLE_CLIENT_ID=...
XV12_GOOGLE_CLIENT_SECRET=...
XV12_GOOGLE_REDIRECT_URI=https://omega.tailce2276.ts.net:10000/api/auth/google/callback
XV12_OWNER_GOOGLE_SUB=...
XV12_COOKIE_SECURE=1

XV12_TAILSCALE_SERVE_ORIGIN=https://omega.tailce2276.ts.net:10000
XV12_TAILSCALE_API_TOKEN=...
XV12_TAILSCALE_TAILNET=tailnet-name.ts.net
XV12_TAILSCALE_ROLE=member
XV12_ONBOARDING_APPROVAL_REQUIRED=1
XV12_ONBOARDING_INVITE_TTL_HOURS=24
```

`XV12_TAILSCALE_API_TOKEN` and `XV12_TAILSCALE_TAILNET` are optional. When both are present, XV12 requests an email-less member invitation from the Tailscale API. When absent or rejected, the Owner UI reports that truthfully and the XV12 invitation remains independently manageable. API credentials are server-only.

The Owner UI presents onboarding as two ordered steps: join the tailnet first, then scan the private XV12 enrollment QR. If Tailscale API automation is unavailable, the UI says that tailnet access must be arranged separately. Do not point onboarding at a public Funnel endpoint.

The preserved Serve topology on this host is:

- `https://omega.tailce2276.ts.net:443` → `127.0.0.1:8084` (Calibration IQ)
- `https://omega.tailce2276.ts.net:8443` → `127.0.0.1:3134` (unrelated existing service)
- `https://omega.tailce2276.ts.net:10000` → `127.0.0.1:8120` (XV12)

Funnel is disabled. XV12 must not reset or replace the other routes.

Register the exact `XV12_GOOGLE_REDIRECT_URI` in the Google OAuth client before testing production sign-in.

## Configure Tailscale Serve

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-tailscale-remote.ps1
```

The script verifies that XV12 listens only on loopback, preserves unrelated Serve routes, and adds only the selected HTTPS route. `-ValidateOnly` performs no change.

## Owner and recipient workflow

1. Sign in as the Owner and open **Settings → Users & Onboarding**.
2. Choose whether approval is required and select **Create invitation**.
3. If Tailscale API automation is configured, share the Step 1 member-invite QR; otherwise add the recipient to the tailnet separately.
4. After the recipient joins the tailnet, share the Step 2 XV12 link or QR. The raw secret is shown once; SQLite stores only its SHA-256 hash.
5. The recipient returns through the private XODUZ URL and selects **Continue with Google**.
6. XV12 carries only a short-lived server-side handoff through the existing OIDC state record. The secret is stripped from the visible URL before Google starts.
7. On callback, XV12 atomically consumes the invitation and binds the verified Google `sub`, email, and name. A replay or identity collision fails closed.
8. If approval is required, no session is issued until the Owner selects **Approve**. Initial capability grants become effective at approval.
9. The recipient can install the Android PWA from **Settings → Install XODUZ** or the browser's **Add to Home screen** command.

Revoking a user disables their XV12 account, invalidates sessions, and removes capability grants. It does not pretend to remove accepted tailnet membership. Revoking an unused XV12 invitation also attempts to delete its matching Tailscale invite when one exists.

## Persistence and audit

Invitation, handoff, OIDC-link, approval, binding, and audit records live in the existing XV12 SQLite database. Invitation statuses are `pending`, `pending_approval`, `active`, `revoked`, or `expired`. The onboarding audit records identifiers and event metadata, never raw invitation secrets, Google tokens, session cookies, or the Tailscale API token.

The service worker caches only versioned application-shell assets. It explicitly bypasses `/api/` and `/onboard/`; authenticated data and invitation URLs are never cached.

Inspect private routing with `tailscale serve status --json`. Do not use `tailscale serve reset`, which could erase routes owned by other local products. Never enable Funnel for XV12.
