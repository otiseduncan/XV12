# ComfyUI photorealistic image provider

## Runtime contract

XV12 integrates ComfyUI as an optional local platform service without importing code or configuration from another XODUZ repository. The default runtime is `X:\AI_Runtimes\ComfyUI_windows_portable`, bound to `127.0.0.1:8188`, with checkpoint `Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors`.

Configuration lives under `media.image` in `config/runtime.json` and can be overridden with the `XV12_COMFYUI_*` variables documented in `.env.example`. The API URL must be loopback and must not include credentials. Default output is 1024 by 1024 with 28 steps, CFG 7, Euler sampling, normal scheduling, and denoise 1.

## Lifecycle and ownership

The normal launcher runs:

```powershell
scripts\xv12-comfyui.ps1 -Action Ensure
scripts\xv12-comfyui.ps1 -Action Status
scripts\xv12-comfyui.ps1 -Action Stop
```

`Ensure` reuses a healthy runtime already listening on the configured port but does not claim ownership of it. If no runtime is present, XV12 validates the embedded Python executable, ComfyUI entrypoint, and configured checkpoint, then starts a hidden loopback-only process and records its exact PID and start time. `Stop` terminates only that matching XV12-owned process. An external or stale process is left running.

Status and `/api/health` report configuration, API reachability, checkpoint availability, ComfyUI version, device name, and ownership without exposing secrets. An occupied but unhealthy port fails closed.

## Provider selection

`media.image.generate` accepts `provider=auto|comfyui|design`.

- `auto` sends ordinary image and realistic scene requests to `comfyui-photorealistic`.
- `auto` sends explicit logo, icon, poster, vector, diagram, infographic, badge, wordmark, typography, brand-mark, and flat-design requests to `xoduz-local-design`.
- `comfyui` and `design` explicitly select a provider.

If ComfyUI is disabled, unreachable, or missing the configured checkpoint, realistic generation returns `unavailable` with provider health and `fallback_used=false`. It creates no artifact and never substitutes a design poster.

## Artifacts and edits

A successful ComfyUI generation is copied into XV12-owned media storage and registered through the existing Artifact Store. Chat receives the same protected image card used by other native artifacts: inline display plus authorized download, stable artifact ID, conversation/user ownership, SHA-256, MIME type, and bounded provider provenance.

This integration implements txt2img only. Genuine ComfyUI image-to-image editing is not configured. Automatic or ComfyUI-selected edits of a ComfyUI image return a truthful unavailable result and create no child artifact. Explicit `provider=design` remains available for the existing SVG composition workflow and records parent linkage.

## Validation

Run the unit/integration contract and lifecycle smoke:

```powershell
runtime\python\Scripts\python.exe -m pytest tests\test_comfyui_integration.py -q
scripts\comfyui-lifecycle-smoke.ps1
scripts\run-regression.ps1 -Pack creator
runtime\python\Scripts\python.exe scripts\check-core-guard.py
```

The lifecycle smoke uses port 18188. It proves XV12 can start, recognize, and stop a process it owns while leaving the primary external runtime on port 8188 untouched.
