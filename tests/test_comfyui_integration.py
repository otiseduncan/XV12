from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from app.comfyui import ComfyUIConfig, ComfyUIProvider
from app.config import ROOT, Settings
from app.creator import MediaService
from .conftest import create_conversation, login


pytestmark = pytest.mark.creator
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


def config(tmp_path: Path, *, enabled: bool = True) -> ComfyUIConfig:
    return ComfyUIConfig(
        enabled=enabled, root=tmp_path / "ComfyUI-portable", port=8188,
        base_url="http://127.0.0.1:8188", checkpoint="Juggernaut.safetensors",
        width=1024, height=1024, timeout_seconds=60, output_path=tmp_path / "outputs",
    )


def transport(*, healthy: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/system_stats":
            if not healthy:
                return httpx.Response(503, json={"error": "offline"})
            return httpx.Response(200, json={"system": {"comfyui_version": "test"}, "devices": [{"name": "test GPU"}]})
        if path == "/object_info/CheckpointLoaderSimple":
            return httpx.Response(200, json={"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["Juggernaut.safetensors"]]}}}})
        if path == "/prompt":
            payload = json.loads(request.content)
            assert payload["prompt"]["4"]["inputs"]["ckpt_name"] == "Juggernaut.safetensors"
            assert payload["prompt"]["5"]["inputs"] == {"width": 1024, "height": 1024, "batch_size": 1}
            return httpx.Response(200, json={"prompt_id": "prompt-1"})
        if path == "/history/prompt-1":
            return httpx.Response(200, json={"prompt-1": {"outputs": {"9": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}}})
        if path == "/view":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        raise AssertionError(f"Unexpected ComfyUI path: {path}")
    return httpx.MockTransport(handler)


def test_settings_parse_comfyui_environment(monkeypatch):
    monkeypatch.setenv("XV12_COMFYUI_ENABLED", "1")
    monkeypatch.setenv("XV12_COMFYUI_ROOT", r"X:\AI_Runtimes\ComfyUI_windows_portable")
    monkeypatch.setenv("XV12_COMFYUI_PORT", "8188")
    monkeypatch.setenv("XV12_COMFYUI_BASE_URL", "http://127.0.0.1:8188")
    monkeypatch.setenv("XV12_COMFYUI_CHECKPOINT", "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors")
    settings = Settings.load()
    assert settings.comfyui_enabled is True and settings.comfyui_port == 8188
    assert settings.comfyui_root == Path(r"X:\AI_Runtimes\ComfyUI_windows_portable")
    assert settings.comfyui_default_width == 1024 and settings.comfyui_default_height == 1024


def test_comfyui_status_and_workflow_truth(tmp_path: Path):
    provider = ComfyUIProvider(config(tmp_path), transport())
    status = provider.status()
    assert status["healthy"] is True and status["checkpoint_available"] is True
    workflow = provider.workflow("automotive calibration shop", "Juggernaut.safetensors", 1024, 1024, 42)
    assert workflow["3"]["inputs"] == {"seed": 42, "steps": 28, "cfg": 7, "sampler_name": "euler", "scheduler": "normal", "denoise": 1, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}
    assert workflow["9"]["class_type"] == "SaveImage"
    unavailable = ComfyUIProvider(config(tmp_path), transport(healthy=False)).status()
    assert unavailable["healthy"] is False and unavailable["status"] == "unavailable"


def test_comfyui_generation_downloads_actual_output(tmp_path: Path):
    provider = ComfyUIProvider(config(tmp_path), transport())
    path, metadata = provider.generate("A futuristic automotive calibration shop", tmp_path / "owned")
    assert path.read_bytes() == PNG and path.suffix == ".png"
    assert metadata["provider"] == "comfyui-photorealistic" and metadata["actual_generation"] is True
    assert metadata["checkpoint"] == "Juggernaut.safetensors" and metadata["prompt_id"] == "prompt-1"


def test_provider_selection_prefers_comfyui_except_explicit_design_requests():
    assert MediaService.select_image_provider("Generate an image of a futuristic automotive calibration shop")[0] == "comfyui-photorealistic"
    assert MediaService.select_image_provider("A cinematic modern ADAS calibration bay with vehicles")[0] == "comfyui-photorealistic"
    assert MediaService.select_image_provider("Generate a logo for Syfernetics")[0] == "xoduz-local-design"
    assert MediaService.select_image_provider("photorealistic shop", "design")[0] == "xoduz-local-design"


def test_api_does_not_silently_fallback_when_comfyui_is_unavailable(app, client, tmp_path: Path):
    login(client, "admin")
    conversation = create_conversation(client)
    app.state.creator_platform.media.comfyui = ComfyUIProvider(config(tmp_path), transport(healthy=False))
    realistic = client.post("/api/capabilities/media.image.generate", json={"arguments": {
        "prompt": "Generate an image of a futuristic automotive calibration shop", "conversation_id": conversation["id"],
    }}).json()["result"]
    assert realistic["status"] == "unavailable" and realistic["provider"] == "comfyui-photorealistic"
    assert realistic["fallback_used"] is False and "artifact" not in realistic
    logo = client.post("/api/capabilities/media.image.generate", json={"arguments": {
        "prompt": "Generate a logo for Syfernetics", "conversation_id": conversation["id"],
    }}).json()["result"]
    assert logo["status"] == "success" and logo["provider"] == "xoduz-local-design"


def test_api_comfyui_generation_registers_chat_image_artifact(app, client, tmp_path: Path):
    login(client, "admin")
    conversation = create_conversation(client)
    provider_config = config(tmp_path)
    provider_config.output_path = app.state.creator_platform.media.media_root / "comfyui-test"
    app.state.creator_platform.media.comfyui = ComfyUIProvider(provider_config, transport())
    result = client.post("/api/capabilities/media.image.generate", json={"arguments": {
        "prompt": "A cinematic automotive calibration bay", "conversation_id": conversation["id"],
    }}).json()["result"]
    artifact = result["artifact"]
    assert result["provider"] == "comfyui-photorealistic" and result["fallback_used"] is False
    assert artifact["type"] == "image" and artifact["mime_type"] == "image/png"
    assert artifact["downloadable"] is True and artifact["metadata"]["checkpoint"] == "Juggernaut.safetensors"
    assert next(provider_config.output_path.rglob("*.png")).read_bytes() == PNG
    delivered = client.get(artifact["reference"])
    assert delivered.status_code == 200 and delivered.content == PNG


def test_launcher_contract_manages_comfyui_as_a_native_xv12_service():
    start = (ROOT / "scripts" / "start-xv12.ps1").read_text(encoding="utf-8")
    stop = (ROOT / "scripts" / "stop-xv12.ps1").read_text(encoding="utf-8")
    status = (ROOT / "scripts" / "status-xv12.ps1").read_text(encoding="utf-8")
    runtime = (ROOT / "scripts" / "xv12-comfyui.ps1").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "comfyui-lifecycle-smoke.ps1").read_text(encoding="utf-8")
    assert 'xv12-comfyui.ps1" -Action Ensure' in start and 'xv12-comfyui.ps1" -Action Stop' in stop
    assert 'xv12-comfyui.ps1" -Action Status' in status
    assert "'127.0.0.1'" in runtime and "managed_by='XV12'" in runtime
    assert "leaving the external runtime running" in runtime and "stop_removed_owned_process" in smoke
    assert "$script:xv12root" in runtime.casefold()


def test_registry_allows_realistic_generation_to_outlive_the_default_tool_timeout(app):
    capability = app.state.registry.capabilities["media.image.generate"]
    assert capability["timeout_seconds"] >= 300
