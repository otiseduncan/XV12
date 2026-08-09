# XODUZ XV12 Functional Assistant Baseline

XV12 is a standalone, local-first XODUZ application. The functional baseline preserves the fast model-first conversation core and adds a permanent bounded shell, trusted conversational identity, optional project context, independent voice dictation and spoken output, live current-information search, verified ADAS knowledge, authenticated Calibration IQ reads, and an admin-only allowlisted Calibration IQ start action.

## Start XODUZ

Double-click `Launch-XODUZ.cmd`.

The launcher verifies its ports, starts the XV12-owned model and application services, validates the exact model alias and context contract, and opens the UI. It never adopts an unverified process on an occupied port.

To stop the application-owned services, double-click `Stop-XODUZ.cmd`.

## One-time local setup

The frozen working baseline already contains its local Python environment and model/runtime assets. On a reconstructed checkout:

1. Place the requested GGUF at the repository-relative path recorded in `config/runtime.json`.
2. Place a compatible llama.cpp Windows build in `runtime/llama.cpp/`.
3. Run `powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1`.
4. Create untracked `config/.env.local` from `.env.example` and set the immutable owner Google `sub`.

Normal startup does not install software or modify Windows.

## Authentication

Production mode is `XV12_AUTH_MODE=google`. The backend implements Google OpenID Connect authorization-code flow with state, nonce, signature, issuer, audience, expiry, verified-email, and single-use attempt validation. Tokens are exchanged server-side; the browser receives only an HttpOnly application session cookie.

Controlled test mode is intentionally limited to three fixed local identities and is enabled only when `XV12_AUTH_MODE=test`. It exists so authentication, sole-admin behavior, isolation, and manual baseline testing remain exercisable when an operator has not registered the XV12 callback URI with Google.

The sole administrator is bound by `XV12_OWNER_GOOGLE_SUB`. Email is profile data, not the permanent identity key. No UI can promote or transfer administrator status.

## Runtime ownership

- UI/backend: `http://127.0.0.1:8120`
- llama-server: `http://127.0.0.1:8121/v1`
- Model alias: `xoduz-qwen3-coder-30b`
- Context: 32,768 tokens
- Application database: `data/xv12.db`
- XV12-owned ADAS database: `data/knowledge/adas_knowledge.sqlite`
- Independent Calibration IQ API: local scoped read-only service
- Attachments: `data/attachments/<internal-user-id>/`
- Operational logs: `logs/`

Every path above is resolved from the XV12 repository root. The standalone audit is executable with `scripts/standalone-audit.ps1`.

## Architecture

Normal conversation goes directly to the local model. Deterministic code owns authentication, authorization, persistence, schema enforcement, context packing, capability execution truth, and observability. There is no intent router, phrase-response system, canned conversation engine, response rewrite layer, or hidden chain-of-thought logging.

Private persistence is centralized in `UserScopedStore`; each conversation, message, summary, active subject, and attachment query requires the internal authenticated `user_id`.

The context order is stable identity, authenticated user, active subject, rolling summary, then recent conversation. Per-turn traces record section and size accounting plus streaming lifecycle timestamps without recording hidden reasoning.

Capability awareness is generated from `config/capabilities.v1.json`. Ordinary conversation remains a direct one-call stream; native model-selected tools enter a bounded function loop only when needed.

## Validation

Run every fast pack:

```powershell
scripts\run-regression.ps1 -Pack all
```

Focused packs include `chat-core`, `ui-shell`, `auth`, `user-identity`, `memory-isolation`, `voice`, `voice-output`, `project-context`, `capability-registry`, `web`, `databases`, `attachments`, and `launcher`.

Run the live production-route acceptance checks while XV12 is running:

```powershell
scripts\acceptance.ps1
```

The original core freeze remains tagged `xv12-baseline-core-v1`, and the initial functional phase remains tagged `xv12-baseline-functional-assistant-v1`. Functional-assistant evidence is under `docs/evidence/`; the voice-output addendum is frozen by `xv12-baseline-functional-assistant-voice-v1`.
