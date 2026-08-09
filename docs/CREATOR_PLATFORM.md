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

## Capability surface

The 4.0.0 registry adds:

- Jobs: `job.status`, `job.cancel`.
- Builder: `builder.workspace.create/open/inspect`, `builder.files.read/patch/batch`, `builder.sandbox.exec`, `builder.preview.start/status/stop`, and `builder.project.archive`.
- Browser validation: `browser.preview.inspect`, `browser.preview.screenshot`.
- Git: `git.status`, `git.diff`, `git.commit`, `git.pull`, `git.push`.
- Secrets: `secrets.reference.configure`, `secrets.reference.status`.
- Media: `media.image.status`, `media.image.generate`, `media.image.edit`, `media.video.generate`.

Every operation still passes through registry schema validation, role/risk authorization, user ownership checks, and the Capability Gateway evidence contract.

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

`tests/test_creator_stack.py` behaviorally verifies registry/health truth, user ownership, path traversal blocking, atomic batches, secret non-disclosure, Docker isolation, explicit network policy, actual application build/test/preview, Chromium DOM/console/runtime/network/click inspection and screenshot, project download, design image/edit lineage, asynchronous playable video generation, cooperative cancellation, job/preview restart reconciliation, Git status/diff/commit/push/pull, and a real chat tool loop that builds then edits and retests the same workspace. `tests/test_comfyui_integration.py` verifies ComfyUI configuration, API health/checkpoint checks, workflow submission, output download, artifact registration, provider selection, truthful failure, and launcher ownership contracts.

Run:

```powershell
runtime\python\Scripts\python.exe -m pytest tests\test_creator_stack.py -q
runtime\python\Scripts\python.exe -m pytest tests\test_comfyui_integration.py -q
runtime\python\Scripts\python.exe -m pytest -q
runtime\python\Scripts\python.exe scripts\check-core-guard.py
```
