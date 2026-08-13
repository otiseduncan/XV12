# XV12 Creator platform

## Architecture

The Creator stack is a modular service layer behind the existing versioned capability registry and execution gateway. It does not change the frozen XODUZ model, identity, context, memory, synthesis, or protected streaming loop.

- `CreatorStore` persists user-owned workspaces, durable jobs, preview ownership, and opaque secret references in SQLite.
- `JobManager` runs bounded background work, persists progress/results, supports cancellation, and reconciles interrupted queued/running jobs as failed after restart.
- `WorkspaceService` creates and reopens managed workspaces, provides bounded reads, optimistic single-file writes, rollback-safe atomic batches, inspection, and secret-excluding archives.
- `SandboxService` executes argv inside Docker with a single owned workspace mount, resource limits, dropped capabilities, read-only container root, no Docker socket, and network disabled by default.
- `PreviewService` owns only labeled Creator preview containers and exposes them on loopback ports.
- `BrowserService` limits Chromium to owned loopback previews and returns bounded DOM, console, JavaScript exception, failed-network, optional-click, and screenshot evidence through the DevTools protocol.
- `GitService` uses fixed argv operations inside owned workspaces. Pull is fast-forward-only and push has no force/refspec interface.
- `SecretsBroker` stores environment-variable references and permitted contexts only. Resolution occurs internally at execution time.
- `MediaService` provides provider-neutral image/edit/video contracts. Generated files enter the existing Artifact Store.
- `BuilderExecutionService` runs complete website/application work behind one high-level chat capability while preserving the ordinary conversation loop's own independent, bounded orchestration (model rounds, operations, wall time, and duplicate-call suppression tracked separately -- see `app/assistant.py`).

## Capability surface

The 4.3.0 registry includes:

- Jobs: `job.status`, `job.cancel`.
- Builder: high-level `builder.session.execute` plus the retained low-level `builder.workspace.create/open/inspect`, `builder.files.read/patch/batch`, `builder.code.search/map`, `builder.task_state.update`, `builder.sandbox.exec`, `builder.preview.start/status/stop`, and `builder.project.archive` primitives.
- Browser validation: `browser.preview.inspect`, `browser.preview.screenshot`.
- Git: `git.status`, `git.diff`, `git.commit`, `git.pull`, `git.push`.
- Secrets: `secrets.reference.configure`, `secrets.reference.status`.
- Media: `media.image.status`, `media.image.generate`, `media.image.edit`, `media.video.generate`.
- Engineering (admin-only, read-only repository inspection for ordinary conversation): `engineering.repo.map`, `engineering.code.search`, `engineering.files.read/batch_read`, `engineering.git.status/diff`, `engineering.tests.inspect`. No shell, writes, Docker, or build/test execution -- that remains Builder's responsibility.
- Files: `files.local.read/write/modify/batch_read`, scoped per authenticated user (admin retains configured repository-wide read roots; normal users are limited to their own managed and attachment areas, with secret/credential paths denied for every role).

Every operation still passes through registry schema validation, role/risk authorization, user ownership checks, and the Capability Gateway evidence contract.

## Durable Builder Execution Sessions

Ordinary X conversation turns use their own bounded orchestrator (`ASSISTANT_MODEL_ROUND_LIMIT`, `ASSISTANT_HARD_OPERATION_LIMIT`, and a wall-time bound in `app/assistant.py`) with a mandatory tools-disabled final synthesis whenever a bound is reached, so a turn that stops early still returns a grounded answer instead of a naked limit message. Complete website and application requests should select `builder.session.execute` once; low-level Builder primitives are no longer advertised to the ordinary conversation model, but remain registered and callable for explicit diagnostics, tests, and advanced operations.

The high-level capability creates or resumes one user-owned workspace and queues a persisted Creator job. The same configured XODUZ/Qwen model then directs a focused internal engineering loop containing only owned workspace, files, sandbox, preview, browser-validation, archive, and read-only Git inspection tools. The deterministic service owns authorization, isolation, persistence, cancellation, evidence gates, and limits; it does not choose the application design or generate a canned template.

Initial limits are 20 soft operations, 32 hard operations, 20 model rounds, six repair cycles, six browser-validation cycles, 1,200 seconds wall time, a Builder-scoped 4,096-token response allowance, and 120,000 characters of bounded working context. The larger response allowance is applied to a cloned Builder model client and does not mutate the ordinary chat model. File reads, sandbox output, DOM text, and observations are truncated before returning to the model; full execution logs remain protected artifacts. Reaching a Builder-specific bound persists `partial_success`, the same workspace, any managed preview, completed files, observations, and a continuation recommendation. It never emits the ordinary chat tool-limit message.

Each session persists its user, conversation, optional project, workspace, parent session, request, mode, job, stage, operation/model/repair/browser counts, bounded observations, elapsed time, preview, artifact, and final status. Hidden reasoning is never stored. Any follow-up in a conversation with an owned prior Builder session resumes that workspace, including a model-mislabeled `build`; `new_workspace=true` is required to intentionally create a distinct project in the same conversation. A first build in a new conversation creates a workspace. Backend restart marks active sessions interrupted rather than succeeded; files remain available for continuation.

Jobs use the existing queued/running/succeeded/failed/cancelled vocabulary. Human-readable progress messages replace internal tool spam. Cancellation is cooperative between bounded operations, stops the session preview when appropriate, and preserves the workspace. The job result is the authoritative completion surface and cannot succeed until real files changed, a sandbox test/build passed, a managed preview exists, and Chromium reports a rendered healthy page. Screenshot and secret-excluding project archive creation are mandatory finalization steps.

Job progress is polled by the authenticated chat card through the owned Creator endpoint, not by the conversation model. `job.status` remains registered and API-callable but is not model-exposed, preventing a queued background build from consuming the ordinary four-round budget. `job.cancel` remains model-visible for natural stop requests.

Browser/test failures remain model-visible bounded observations. A write after unhealthy browser evidence counts as a repair cycle, and the model must rerun validation. The final application artifact records validation, screenshot fallback, project archive, stable workspace identity, and stable preview identity.

The deterministic controller reports remaining verification gates after every operation batch. Any later file write invalidates earlier sandbox and browser proof, while redundant inspection of an unchanged already-healthy preview is suppressed. This keeps the model in charge of engineering decisions without allowing repeated checks to consume the browser-cycle budget or stale evidence to authorize success.

After technical gates pass, the same configured Builder model performs a bounded structured acceptance review comparing the original request, final Chromium-visible UI, and recent engineering receipts. Any explicit missing section, behavior, content, or visual change is returned to the engineering loop for repair, retest, and revalidation; technical health alone cannot authorize completion.

Managed previews are network-contained. The Builder model is instructed to generate self-contained local assets instead of relying on internet images, fonts, scripts, stylesheets, APIs, or CDNs; blocked external requests therefore remain a real validation failure rather than being rubber-stamped.

## Inline application preview security

Application cards embed only an owner-issued `/api/creator/previews/{preview_id}/token/{unguessable_token}/...` path. The preview-scoped bearer path lets relative CSS, JavaScript, images, forms, and navigation work inside the opaque-origin sandbox without exposing the parent's session cookie. Owner-authenticated API access remains available; guessed IDs or tokens fail, stopped previews revoke access, and both routes resolve only the exact stored loopback port, reject traversal and all redirects, and cannot proxy arbitrary URLs. Responses add frame/content security headers. The iframe allows scripts, forms, and modals for safe interaction but omits same-origin and top-navigation privileges, preventing preview code from accessing the parent Conversation Bay DOM.

The interactive preview is mandatory and contained to a bounded card height. A verified screenshot is retained as fallback, while Open / Expand remains secondary. Download Project points to the separately authorized archive artifact. The polling job card automatically adds the live application to the originating conversation when the build finishes; no extra “show it” prompt or external Builder dashboard is required.

## Chat artifacts and jobs

Artifact schema 3 adds explicit Creator kinds and parent linkage while preserving stable IDs and conversation/user authorization. The generic renderer supports:

- inline image and screenshot cards;
- native playable video cards;
- sandboxed application preview cards;
- test/build report and Git receipt cards;
- downloadable project archives;
- persisted job cards with progress, cancellation, and result replacement.

Only bounded summaries enter ordinary chat. Full receipts remain protected artifact downloads. Internal paths, container references, secret reference environment names, and secret values are not public metadata.

## Providers

`comfyui-photorealistic` is the default provider for ordinary image and realistic-scene requests. It submits a native txt2img workflow to the configured loopback ComfyUI API, verifies the configured checkpoint, downloads the actual output, and registers it as a protected inline/downloadable chat image with checkpoint, seed, dimensions, workflow, and provider provenance.

`xoduz-local-design` remains a credential-free SVG provider for explicit logo, icon, poster, vector, diagram, wordmark, and similar graphic-design requests. Provider selection can be overridden with `provider=comfyui` or `provider=design`. An unhealthy ComfyUI service never silently downgrades a realistic request to this provider.

ComfyUI image-to-image editing is not configured in this release. Automatic or explicit ComfyUI edits return a truthful unavailable result and create no artifact. A caller may explicitly choose the design provider for a derived SVG composition that preserves parent artifact IDs.

`xoduz-local-ffmpeg` is the default video provider when FFmpeg is installed. It renders the owned source image, encodes an H.264 MP4 in a background job, reports progress, supports cancellation state, and returns a playable video artifact linked to its source image. When FFmpeg or Chromium is absent, capability health/result truthfully reports unavailable/failure.

The secrets registry never stores provider credentials. Additional providers can resolve pre-authorized secret references internally without changing the model-visible contract.

## Permissions and risk

- User grants can independently enable Builder read/write/modify/execute, Browser Validation read, Media modify/execute, Jobs read/modify, Git read, and Secrets status read.
- Registry risk tier 2 blocks normal users even if a grant is attempted.
- Git commit/pull/push and secret-reference configuration are administrator-only role ceilings.
- The current admin permissions UI remains registry-driven; no Creator-specific permission screen is required.

## Security and network policy

Builder commands receive no unrestricted host shell. Docker mounts only the exact owned workspace at `/workspace`; the repository root, another user's workspace, host filesystem, and Docker socket are not mounted. The container root is read-only, temporary space is bounded, capabilities are dropped, `no-new-privileges` is set, and CPU/memory/PID limits are enforced.

Network mode is `none` by default. A capability call must explicitly set `network=true`, which is intended for dependency installation or other user-requested network work. Tests verify the network namespace changes only under that explicit flag.

Secret values are inherited by Docker only for named, configured, context-authorized references. Values are passed through the subprocess environment rather than command arguments and are redacted from bounded observations and full receipt artifacts. Other host environment values are not forwarded into the container.

## Persistence, recovery, and cleanup

Creator workspaces and completed artifacts are durable user-owned state and are not automatically deleted. Preview containers are individually owned and stopped only by exact preview ID. Temporary browser profiles and intermediate video source frames are removed after use. Archives exclude `.git`, dependency directories, `.env*`, Creator receipt/archive directories, and secret files.

On service startup, jobs left queued/running/cancelling are marked failed with `service_restarted`; they are never falsely reported as complete. Managed preview records are reconciled against their exact owned containers and stale records become stopped. The original workspace and any already-registered artifacts remain intact for inspection or retry.

Backup the Creator SQLite database with SQLite's backup API and copy the Creator workspace/media tree while XV12 is stopped or after files have quiesced. Verify the database with `PRAGMA integrity_check`, verify every SHA-256 manifest entry, and use the repository Git bundle as the history recovery source.

## Validation

`tests/test_creator_stack.py` behaviorally verifies the low-level Creator primitives and machine integrations. `tests/test_builder_execution_session.py` portably verifies one-call chat delegation, a model-directed loop exceeding four operations, the 32-operation hard bound, workspace/preview/artifact continuation, injected browser-failure repair, progress, cancellation, restart reconciliation, cross-user denial, secure preview proxying, inline UI contracts, and the unchanged ordinary chat bound. `tests/test_comfyui_integration.py` verifies ComfyUI configuration, API health/checkpoint checks, workflow submission, output download, artifact registration, provider selection, truthful failure, and launcher ownership contracts.

Run:

```powershell
runtime\python\Scripts\python.exe -m pytest tests\test_creator_stack.py -q
runtime\python\Scripts\python.exe -m pytest tests\test_builder_execution_session.py -q
runtime\python\Scripts\python.exe -m pytest tests\test_comfyui_integration.py -q
runtime\python\Scripts\python.exe -m pytest -q
runtime\python\Scripts\python.exe scripts\check-core-guard.py
```
