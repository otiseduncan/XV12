# XODUZ XV12 Functional Assistant Baseline

XV12 is a standalone, local-first XODUZ application. The functional baseline preserves the fast model-first conversation core and adds a permanent bounded shell, trusted conversational identity, optional project context, independent voice dictation and spoken output, live current-information search, verified ADAS knowledge, authenticated Calibration IQ reads, an admin-only allowlisted Calibration IQ start action, and the conversation-native Creator platform.

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

Private remote access through Tailscale Serve is optional. Tailscale supplies reachability only; Google OIDC remains XV12 authentication. New Google identities enroll through hashed, one-time Owner invitations, can require approval, and receive explicit capability grants. The Android-installable PWA includes the real XODUZ icon and never caches APIs or invitation URLs. See `docs/XV12_TAILSCALE_REMOTE_ACCESS.md`. XV12 does not use Tailscale Funnel.

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
- Creator state: `data/capabilities/creator/` (jobs, workspaces, previews, media, and receipts)

Every path above is resolved from the XV12 repository root. The standalone audit is executable with `scripts/standalone-audit.ps1`.

## Architecture

Normal conversation goes directly to the local model. Deterministic code owns authentication, authorization, persistence, schema enforcement, context packing, capability execution truth, and observability. There is no intent router, phrase-response system, canned conversation engine, response rewrite layer, or hidden chain-of-thought logging.

Private persistence is centralized in `UserScopedStore`; each conversation, message, summary, active subject, and attachment query requires the internal authenticated `user_id`.

The context order is stable identity, authenticated user, active subject, rolling summary, then recent conversation. Per-turn traces record section and size accounting plus streaming lifecycle timestamps without recording hidden reasoning.

Capability awareness is generated from `config/capabilities.v1.json`. Ordinary conversation remains a direct one-call stream; native model-selected tools enter a bounded function loop only when needed.

## Creator platform

X can create and edit prompt-driven images, turn an owned image artifact into a playable video job, and build complete applications through a durable model-directed Builder Execution Session. A complex build enters one high-level chat capability, then uses its own bounded engineering loop for workspace files, isolated dependency/build/test execution, evidence-driven repair, managed preview startup, Chromium validation, screenshots, and project packaging. Follow-up edits reuse stable workspace, preview, and artifact IDs without weakening the ordinary four-round conversation limit.

Creator outputs use the generic chat artifact renderer: images and screenshots display inline, videos have native playback, and Builder applications render as interactive opaque-origin sandboxed previews through an owner-issued, unguessable per-preview proxy path. One compact job card polls persisted progress, supports cancellation, and automatically attaches the verified application and Download Project action when complete. Normal chat shows bounded summaries rather than raw container or process details.

The `comfyui-photorealistic` provider uses the configured loopback ComfyUI runtime and Juggernaut XL checkpoint for ordinary realistic image requests. The built-in `xoduz-local-design` provider handles explicit logo, icon, poster, vector, diagram, and similar design requests. If ComfyUI is unavailable, realistic requests fail truthfully and are never silently replaced by a design poster. The local video provider uses FFmpeg and reports unavailable if FFmpeg is absent. Secret values are supplied only through externally configured environment variables and opaque references; values are never accepted by the registry, returned to the model, stored in artifact metadata, or written unredacted to execution receipts.

Builder execution has no host shell. Commands run as argv inside a resource-limited Docker container with only the owned workspace mounted, Docker capabilities dropped, `no-new-privileges`, and networking disabled unless the capability call explicitly enables it. Git commit/pull/push and secret-reference configuration remain administrator-only through the registry role ceiling.

See `docs/CREATOR_PLATFORM.md` and `docs/COMFYUI_IMAGE_PROVIDER.md` for capability, provider, lifecycle, permission, recovery, cleanup, and test details.

## Validation

Run every fast pack:

```powershell
scripts\run-regression.ps1 -Pack all
```

Focused packs include `chat-core`, `ui-shell`, `auth`, `user-identity`, `memory-isolation`, `voice`, `voice-output`, `project-context`, `capability-registry`, `web`, `databases`, `attachments`, `artifacts`, `creator`, and `launcher`.

Run the live production-route acceptance checks while XV12 is running:

```powershell
scripts\acceptance.ps1
```

The original core freeze remains tagged `xv12-baseline-core-v1`, and the initial functional phase remains tagged `xv12-baseline-functional-assistant-v1`. Functional-assistant evidence is under `docs/evidence/`; the voice-output addendum is frozen by `xv12-baseline-functional-assistant-voice-v1`.
