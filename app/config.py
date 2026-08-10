from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    path = ROOT / "config" / ".env.local"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_local_env()


@dataclass(slots=True)
class Settings:
    root: Path
    app_host: str
    app_port: int
    model_port: int
    model_alias: str
    model_context_tokens: int
    model_max_tokens: int
    model_temperature: float
    database_path: Path
    attachments_path: Path
    auth_mode: str
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    owner_google_sub: str
    cookie_secure: bool
    session_ttl_seconds: int = 60 * 60 * 24 * 14
    adas_database_path: Path | None = None
    calibration_iq_base_url: str = "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq"
    calibration_iq_project_path: Path = Path(r"X:\calibration iq")
    web_timeout_seconds: int = 20
    comfyui_enabled: bool = True
    comfyui_root: Path = Path(r"X:\AI_Runtimes\ComfyUI_windows_portable")
    comfyui_port: int = 8188
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_checkpoint: str = "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"
    comfyui_default_width: int = 1024
    comfyui_default_height: int = 1024
    comfyui_timeout_seconds: int = 300
    comfyui_output_path: Path = ROOT / "data" / "capabilities" / "creator" / "media" / "comfyui"
    tailscale_serve_origin: str = ""
    tailscale_api_token: str = ""
    tailscale_tailnet: str = ""
    tailscale_role: str = "member"
    onboarding_base_url: str = ""
    onboarding_approval_required: bool = True
    onboarding_invite_ttl_hours: int = 24

    @property
    def model_base_url(self) -> str:
        return os.getenv("XV12_MODEL_BASE_URL", f"http://127.0.0.1:{self.model_port}/v1")

    @classmethod
    def load(cls) -> "Settings":
        data = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
        app = data["application"]
        model = data["model"]
        storage = data["storage"]
        image = (data.get("media") or {}).get("image") or {}
        comfy_port = int(os.getenv("XV12_COMFYUI_PORT", image.get("port", 8188)))
        auth_mode = os.getenv("XV12_AUTH_MODE", "google").strip().lower()
        owner_sub = os.getenv("XV12_OWNER_GOOGLE_SUB", "").strip()
        if not owner_sub:
            raise RuntimeError("XV12_OWNER_GOOGLE_SUB must bind the immutable sole administrator")
        return cls(
            root=ROOT,
            app_host=os.getenv("XV12_APP_HOST", app["host"]),
            app_port=int(os.getenv("XV12_APP_PORT", app["port"])),
            model_port=int(os.getenv("XV12_MODEL_PORT", model["port"])),
            model_alias=os.getenv("XV12_MODEL_ALIAS", model["alias"]),
            model_context_tokens=int(os.getenv("XV12_MODEL_CONTEXT_TOKENS", model["context_tokens"])),
            model_max_tokens=int(os.getenv("XV12_MODEL_MAX_TOKENS", model["max_response_tokens"])),
            model_temperature=float(os.getenv("XV12_MODEL_TEMPERATURE", model["temperature"])),
            database_path=ROOT / os.getenv("XV12_DATABASE_PATH", storage["database"]),
            attachments_path=ROOT / os.getenv("XV12_ATTACHMENTS_PATH", storage["attachments"]),
            auth_mode=auth_mode,
            google_client_id=os.getenv("XV12_GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.getenv("XV12_GOOGLE_CLIENT_SECRET", "").strip(),
            google_redirect_uri=os.getenv("XV12_GOOGLE_REDIRECT_URI", "").strip(),
            owner_google_sub=owner_sub,
            cookie_secure=os.getenv("XV12_COOKIE_SECURE", "1").strip() not in {"0", "false", "False"},
            adas_database_path=ROOT / os.getenv("XV12_ADAS_DATABASE_PATH", storage["adas_database"]),
            calibration_iq_base_url=os.getenv(
                "XV12_CALIBRATION_IQ_BASE_URL",
                "http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq",
            ).rstrip("/"),
            calibration_iq_project_path=Path(
                os.getenv("XV12_CALIBRATION_IQ_PROJECT_PATH", r"X:\calibration iq")
            ),
            web_timeout_seconds=int(os.getenv("XV12_WEB_TIMEOUT_SECONDS", "20")),
            comfyui_enabled=os.getenv("XV12_COMFYUI_ENABLED", str(image.get("enabled", True))).strip().casefold() not in {"0", "false", "no", "off"},
            comfyui_root=Path(os.getenv("XV12_COMFYUI_ROOT", image.get("root", r"X:\AI_Runtimes\ComfyUI_windows_portable"))),
            comfyui_port=comfy_port,
            comfyui_base_url=os.getenv("XV12_COMFYUI_BASE_URL", image.get("base_url", f"http://127.0.0.1:{comfy_port}")),
            comfyui_checkpoint=os.getenv("XV12_COMFYUI_CHECKPOINT", image.get("checkpoint", "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors")),
            comfyui_default_width=int(os.getenv("XV12_COMFYUI_DEFAULT_WIDTH", image.get("default_width", 1024))),
            comfyui_default_height=int(os.getenv("XV12_COMFYUI_DEFAULT_HEIGHT", image.get("default_height", 1024))),
            comfyui_timeout_seconds=int(os.getenv("XV12_COMFYUI_TIMEOUT_SECONDS", image.get("timeout_seconds", 300))),
            comfyui_output_path=ROOT / os.getenv("XV12_COMFYUI_OUTPUT_PATH", image.get("output_path", "data/capabilities/creator/media/comfyui")),
            tailscale_serve_origin=os.getenv("XV12_TAILSCALE_SERVE_ORIGIN", "").strip().rstrip("/"),
            tailscale_api_token=os.getenv("XV12_TAILSCALE_API_TOKEN", "").strip(),
            tailscale_tailnet=os.getenv("XV12_TAILSCALE_TAILNET", "").strip(),
            tailscale_role=os.getenv("XV12_TAILSCALE_ROLE", "member").strip().casefold(),
            onboarding_base_url=os.getenv("XV12_ONBOARDING_BASE_URL", "").strip().rstrip("/"),
            onboarding_approval_required=os.getenv("XV12_ONBOARDING_APPROVAL_REQUIRED", "1").strip().casefold() in {"1", "true", "yes", "on"},
            onboarding_invite_ttl_hours=max(1, min(168, int(os.getenv("XV12_ONBOARDING_INVITE_TTL_HOURS", "24")))),
        )
