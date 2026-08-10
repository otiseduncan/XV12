# XV12 QR onboarding and installable phone app

XV12 now supports one-time, email-free user onboarding built around the existing Google OIDC and capability-permission architecture.

## Security boundaries

The flow deliberately keeps four separate boundaries:

1. **Tailscale user invitation** decides who can join the private network. XV12 creates a Tailscale `member` invitation without an email address.
2. **Google OIDC** remains the identity provider for Xoduz. The protected XV12 authentication core is unchanged.
3. **XV12 capability grants** remain server-authoritative. A newly provisioned normal user starts with no capability grants, which is the chat-only baseline until the administrator explicitly grants tools.
4. **PWA installation** supplies the Xoduz application name/icon and standalone phone window. It does not replace authentication.

The QR/bootstrap page is intentionally separate from the private Xoduz backend. Only the small bootstrap service on loopback port `8122` should be exposed publicly. Do **not** Funnel port `8120`.

## One-time operator configuration

Put real secrets only in `config/.env.local`.

```env
XV12_AUTH_MODE=google
XV12_GOOGLE_CLIENT_ID=<google client id>
XV12_GOOGLE_CLIENT_SECRET=<google client secret>
XV12_OWNER_GOOGLE_SUB=<owner Google sub>
XV12_COOKIE_SECURE=1

XV12_TAILSCALE_API_TOKEN=<user-owned Tailscale API token>
XV12_TAILSCALE_TAILNET=-
XV12_PRIVATE_BASE_URL=https://<xoduz-node>.<tailnet>.ts.net
XV12_PUBLIC_ONBOARDING_BASE_URL=https://<xoduz-node>.<tailnet>.ts.net:8443
XV12_ONBOARDING_PORT=8122

XV12_GOOGLE_REDIRECT_URI=https://<xoduz-node>.<tailnet>.ts.net/api/auth/google/callback
```

Register the same HTTPS callback URI in the Google OAuth client.

The Tailscale API token must be a **user-owned API access token** because Tailscale's user-invite endpoint requires an inviting user. XV12 never writes that token to its database or returns it to the browser.

## Tailscale routing

Xoduz stays private on its normal backend port. The onboarding bootstrap is the only service intended for public Funnel exposure.

```powershell
tailscale serve --bg 8120
tailscale funnel --bg --https=8443 8122

tailscale serve status
tailscale funnel status
```

Use the exact HTTPS origins reported by those status commands for `XV12_PRIVATE_BASE_URL` and `XV12_PUBLIC_ONBOARDING_BASE_URL`.

Apply a tailnet ACL/grants policy that allows invited normal users to reach the Xoduz node/service they need and does not unintentionally grant access to unrelated tailnet devices.

## Administrator workflow

1. Start XV12 normally. The launcher now also starts the loopback-only onboarding bootstrap on port `8122`.
2. Sign in as the sole Xoduz administrator.
3. Open **Invite User** in the sidebar.
4. Choose the invitation lifetime and click **Generate QR invitation**.
5. XV12 calls Tailscale's user-invite API with role `member` and no email field, creates a one-time local onboarding token, and renders a QR code.
6. Send the QR image or setup link to the user.

A manual Tailscale invite URL can be pasted as a fallback when API automation is not configured.

## User workflow

The QR opens the public-safe bootstrap page and walks the user through:

1. Install/open Tailscale.
2. Accept the one-time Tailscale invitation and connect.
3. Continue to the private Xoduz URL.
4. Sign in with Google.
5. XV12 associates the one-time setup invitation with the now-authenticated Xoduz user.
6. Install Xoduz as an app. The manifest uses the Xoduz icon and `display: standalone`.

The administrator does not need the user's email in advance. Google supplies the verified identity during normal Xoduz sign-in.

## Invitation persistence

`data/capabilities/permissions.sqlite` adds a separate versioned `onboarding_invitations` store beside the unchanged capability-grant schema. Only a SHA-256 hash of the Xoduz invitation token is persisted. The Tailscale invite URL is stored locally because the public-safe bootstrap must present it to the invitation holder. The Tailscale API access token remains environment-only.

Invitation states are `pending`, `active`, `expired`, and `revoked`. A claimed invitation records the authenticated Xoduz user ID and Google identity metadata for administrator auditing.

## Pull-time dependency refresh

This revision adds the locked `qrcode` dependency for server-side SVG QR rendering. After pulling, run once:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

Normal XV12 startup remains non-installing after the dependency refresh.
