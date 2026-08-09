from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from app.config import ROOT
from .conftest import login


@pytest.mark.registry_gateway
def test_registry_is_versioned_and_gateway_executes_registered_tier_zero(client):
    login(client, "admin")
    listing = client.get("/api/capabilities").json()
    assert listing["registry_version"] == "3.0.0"
    ids = {item["id"] for item in listing["capabilities"]}
    assert {"system.health.read", "web.current.search", "adas.coverage.read", "project.list", "settings.voice.read", "settings.voice.update"} <= ids
    assert "service.calibration_iq.start" in ids
    result = client.post("/api/capabilities/system.health.read", json={"arguments": {}})
    assert result.status_code == 200
    assert result.json()["authorization"]["allowed"] is True


@pytest.mark.authorization
@pytest.mark.registry_gateway
def test_normal_user_can_never_execute_tier_two(client):
    login(client, "user-a")
    response = client.post("/api/capabilities/admin.capabilities.inspect", json={"arguments": {}})
    assert response.status_code == 403


@pytest.mark.model_runtime
def test_health_verifies_expected_model_alias_context_and_owned_paths(client):
    login(client)
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["model"]["expected_alias"] == "xoduz-qwen3-coder-30b"
    assert health["model"]["context_tokens"] == 32768
    assert health["model"]["executable_owned"] is True
    assert health["model"]["model_owned"] is True


@pytest.mark.launcher
def test_runtime_configuration_uses_only_xv12_relative_paths():
    config = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
    for path in (config["model"]["executable"], config["model"]["path"], config["storage"]["database"], config["storage"]["attachments"], config["storage"]["adas_database"]):
        assert not Path(path).is_absolute()
        assert (ROOT / path).resolve().is_relative_to(ROOT.resolve())
    assert (ROOT / "Launch-XODUZ.cmd").exists()


@pytest.mark.launcher
def test_standalone_dependency_audit_executes_and_passes():
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "standalone-audit.ps1")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["result"] == "PASS"
