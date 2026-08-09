# Google OIDC operator configuration

Register this callback URI in the operator's Google OAuth client:

`http://127.0.0.1:8120/api/auth/google/callback`

Then place these values in the untracked `config/.env.local`:

```text
XV12_AUTH_MODE=google
XV12_GOOGLE_CLIENT_ID=...
XV12_GOOGLE_CLIENT_SECRET=...
XV12_GOOGLE_REDIRECT_URI=http://127.0.0.1:8120/api/auth/google/callback
XV12_OWNER_GOOGLE_SUB=...
XV12_COOKIE_SECURE=0
```

For an HTTPS deployment, use its exact registered HTTPS callback and set `XV12_COOKIE_SECURE=1`.

The application never asks for or stores Google passwords. Client secrets remain untracked. Changing the owner binding is an operator configuration action, not a UI operation.
