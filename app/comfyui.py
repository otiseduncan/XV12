from __future__ import annotations

import hashlib
import mimetypes
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


NEGATIVE_PROMPT = (
    "low quality, blurry, soft focus, distorted, malformed anatomy, duplicate subject, duplicate face, "
    "extra limbs, extra fingers, bad hands, noisy, oversaturated, watermark, signature, logo, caption, text artifacts"
)
POSITIVE_ENHANCER = "high quality, detailed, photorealistic, cinematic lighting, polished composition, natural materials, professional photography"


@dataclass(slots=True)
class ComfyUIConfig:
    enabled: bool
    root: Path
    port: int
    base_url: str
    checkpoint: str
    width: int
    height: int
    timeout_seconds: int
    output_path: Path

    @classmethod
    def from_settings(cls, settings: Any) -> "ComfyUIConfig":
        return cls(
            enabled=bool(settings.comfyui_enabled), root=Path(settings.comfyui_root).resolve(),
            port=int(settings.comfyui_port), base_url=str(settings.comfyui_base_url).rstrip("/"),
            checkpoint=str(settings.comfyui_checkpoint), width=int(settings.comfyui_default_width),
            height=int(settings.comfyui_default_height), timeout_seconds=int(settings.comfyui_timeout_seconds),
            output_path=Path(settings.comfyui_output_path).resolve(),
        )

    def validate(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("ComfyUI must use a loopback HTTP endpoint.")
        if parsed.username or parsed.password:
            raise ValueError("ComfyUI base URL must not contain credentials.")
        if not (1 <= self.port <= 65535):
            raise ValueError("ComfyUI port is invalid.")
        if not (256 <= self.width <= 2048 and 256 <= self.height <= 2048):
            raise ValueError("ComfyUI default dimensions must be between 256 and 2048.")
        if not (30 <= self.timeout_seconds <= 1800):
            raise ValueError("ComfyUI timeout must be between 30 and 1800 seconds.")


class ComfyUIProvider:
    provider_id = "comfyui-photorealistic"

    def __init__(self, config: ComfyUIConfig, transport: httpx.BaseTransport | None = None) -> None:
        config.validate()
        self.config = config
        self.transport = transport

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.config.base_url, timeout=timeout or self.config.timeout_seconds, transport=self.transport)

    @staticmethod
    def workflow(prompt: str, checkpoint: str, width: int, height: int, seed: int) -> dict[str, Any]:
        return {
            "3": {"class_type": "KSampler", "inputs": {
                "seed": seed, "steps": 28, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1,
                "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
            }},
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": checkpoint}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": f"{prompt}, {POSITIVE_ENHANCER}", "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": NEGATIVE_PROMPT, "clip": ["4", 1]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "xv12_xoduz", "images": ["8", 0]}},
        }

    def status(self) -> dict[str, Any]:
        runtime = {
            "root_configured": self.config.root.is_dir(),
            "python_present": (self.config.root / "python_embeded" / "python.exe").is_file(),
            "main_present": (self.config.root / "ComfyUI" / "main.py").is_file(),
            "checkpoint_file_present": (self.config.root / "ComfyUI" / "models" / "checkpoints" / self.config.checkpoint).is_file(),
        }
        if not self.config.enabled:
            return {"provider": self.provider_id, "status": "disabled", "configured": False, "healthy": False,
                    "checkpoint": self.config.checkpoint, "size": f"{self.config.width}x{self.config.height}", "runtime": runtime}
        configured = bool(self.config.base_url and self.config.checkpoint)
        api_reachable = checkpoint_available = False
        comfy_version = device = ""
        error = ""
        if configured:
            try:
                with self._client(5) as client:
                    stats = client.get("/system_stats")
                    stats.raise_for_status()
                    body = stats.json()
                    api_reachable = True
                    comfy_version = str((body.get("system") or {}).get("comfyui_version") or "")
                    devices = body.get("devices") or []
                    device = str((devices[0] if devices else {}).get("name") or "")
                    object_info = client.get("/object_info/CheckpointLoaderSimple")
                    object_info.raise_for_status()
                    choices = (((object_info.json().get("CheckpointLoaderSimple") or {}).get("input") or {}).get("required") or {}).get("ckpt_name") or [[]]
                    checkpoint_available = self.config.checkpoint in (choices[0] if choices else [])
            except Exception as exc:
                error = type(exc).__name__
        healthy = configured and api_reachable and checkpoint_available
        state = "healthy" if healthy else "unavailable" if configured else "not_configured"
        return {
            "provider": self.provider_id, "status": state, "configured": configured, "healthy": healthy,
            "api_reachable": api_reachable, "checkpoint": self.config.checkpoint,
            "checkpoint_available": checkpoint_available, "size": f"{self.config.width}x{self.config.height}",
            "comfyui_version": comfy_version, "device": device, "runtime": runtime, "error": error or None,
        }

    def generate(self, prompt: str, output_dir: Path, *, width: int | None = None, height: int | None = None) -> tuple[Path, dict[str, Any]]:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("Image prompt is required.")
        state = self.status()
        if not state["healthy"]:
            raise RuntimeError(f"ComfyUI provider is {state['status']}.")
        width = min(max(int(width or self.config.width), 256), 2048)
        height = min(max(int(height or self.config.height), 256), 2048)
        seed = secrets.randbits(63)
        client_id = str(uuid.uuid4())
        with self._client() as client:
            response = client.post("/prompt", json={"prompt": self.workflow(prompt, self.config.checkpoint, width, height, seed), "client_id": client_id})
            response.raise_for_status()
            prompt_id = str(response.json().get("prompt_id") or "")
            if not prompt_id:
                raise RuntimeError("ComfyUI returned no prompt ID.")
            deadline = time.monotonic() + self.config.timeout_seconds
            output: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                history = client.get(f"/history/{prompt_id}")
                history.raise_for_status()
                record = history.json().get(prompt_id) or {}
                status = record.get("status") or {}
                if status.get("status_str") == "error":
                    raise RuntimeError("ComfyUI reported a workflow execution error.")
                for node in (record.get("outputs") or {}).values():
                    images = node.get("images") or []
                    if images:
                        output = images[0]
                        break
                if output:
                    break
                time.sleep(1)
            if not output:
                raise TimeoutError("ComfyUI generation timed out before producing an image.")
            image = client.get("/view", params={
                "filename": str(output.get("filename") or ""), "subfolder": str(output.get("subfolder") or ""),
                "type": str(output.get("type") or "output"),
            })
            image.raise_for_status()
            content = image.content
            if not content or len(content) > 64 * 1024 * 1024:
                raise RuntimeError("ComfyUI returned an invalid image payload.")
            mime_type = image.headers.get("content-type", "").split(";", 1)[0]
            if not mime_type.startswith("image/"):
                raise RuntimeError("ComfyUI output was not an image.")
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = mimetypes.guess_extension(mime_type) or Path(str(output.get("filename") or "generated.png")).suffix or ".png"
        target = output_dir / f"comfyui-{uuid.uuid4().hex}{suffix}"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
        return target, {
            "provider": self.provider_id, "checkpoint": self.config.checkpoint, "width": width, "height": height,
            "seed": seed, "steps": 28, "cfg": 7, "sampler": "euler", "scheduler": "normal",
            "prompt_id": prompt_id, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "actual_generation": True,
        }
