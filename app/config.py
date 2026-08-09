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

    @property
    def model_base_url(self) -> str:
        return os.getenv("XV12_MODEL_BASE_URL", f"http://127.0.0.1:{self.model_port}/v1")

    @classmethod
    def load(cls) -> "Settings":
        data = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
        app = data["application"]
        model = data["model"]
        storage = data["storage"]
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
        )
