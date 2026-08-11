from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.config import ROOT


pytestmark = pytest.mark.launcher
MODEL_SCRIPT = ROOT / "scripts" / "xv12-model.ps1"
BACKEND_SCRIPT = ROOT / "scripts" / "xv12-backend.ps1"
START_SCRIPT = ROOT / "scripts" / "start-xv12.ps1"
COMMON_SCRIPT = ROOT / "scripts" / "xv12-common.ps1"

SERVER = r"""
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
port = int(os.environ['XV12_FIXTURE_PORT'])
kind = os.environ['XV12_FIXTURE_KIND']
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if kind == 'model' and self.path == '/v1/models':
            body = {'data':[{'id':'xoduz-qwen3-coder-30b','meta':{'n_ctx':32768}}]}
        elif kind == 'backend' and self.path == '/api/health':
            body = {'ok':True,'application':{'name':'XODUZ XV12'},'model':{'alias_ok':True,'context_tokens':32768}}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_listening(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"fixture did not listen on {port}")


def listener_pid(port: int) -> int:
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", f"(Get-NetTCPConnection -State Listen -LocalPort {port} | Select-Object -First 1).OwningProcess"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    assert result.returncode == 0 and result.stdout.strip(), result.stdout + result.stderr
    return int(result.stdout.strip())


def fixture_server(kind: str, port: int, identity: list[str]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(XV12_FIXTURE_PORT=str(port), XV12_FIXTURE_KIND=kind)
    process = subprocess.Popen(
        [sys.executable, "-c", SERVER, *identity], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )
    wait_listening(port)
    return process


def lifecycle_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["XV12_STATE_DIRECTORY"] = str(tmp_path / "state")
    env["XV12_LOG_DIRECTORY"] = str(tmp_path / "logs")
    return env


def run_ps(script: Path, action: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", action],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )


def terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def test_clean_startup_contract_launches_model_then_backend_and_uses_a_mutex():
    start = START_SCRIPT.read_text(encoding="utf-8")
    assert "Local\\XODUZ_XV12_Launcher" in start
    assert start.index("xv12-model.ps1") < start.index("xv12-backend.ps1")
    assert "MODEL LAUNCH:" in MODEL_SCRIPT.read_text(encoding="utf-8")
    assert "BACKEND LAUNCH:" in BACKEND_SCRIPT.read_text(encoding="utf-8")


def test_reuses_already_healthy_verified_model_and_recovers_missing_state(tmp_path: Path):
    port = free_port()
    model_path = ROOT / "config" / "runtime.json"
    identity = [str(model_path), "--alias", "xoduz-qwen3-coder-30b", "--port", str(port), "-c", "32768"]
    process = fixture_server("model", port, identity)
    try:
        env = lifecycle_env(tmp_path)
        env.update(XV12_MODEL_PORT=str(port), XV12_MODEL_EXECUTABLE=sys._base_executable, XV12_MODEL_PATH=str(model_path))
        result = run_ps(MODEL_SCRIPT, "Ensure", env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "MODEL STATE RECOVERY" in result.stdout and "MODEL REUSE" in result.stdout
        state = json.loads((tmp_path / "state" / "model.json").read_text(encoding="utf-8-sig"))
        assert state["pid"] == listener_pid(port) and state["context_tokens"] == 32768
    finally:
        terminate(process)


def test_stale_pid_state_is_removed_without_stopping_reused_foreign_pid(tmp_path: Path):
    port = free_port()
    process = fixture_server("model", port, ["foreign-process"])
    try:
        env = lifecycle_env(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "model.json").write_text(json.dumps({
            "root": str(ROOT), "pid": process.pid, "started_at": "2000-01-01T00:00:00Z", "executable": sys._base_executable,
        }), encoding="utf-8")
        env.update(XV12_MODEL_PORT=str(port), XV12_MODEL_EXECUTABLE=sys._base_executable, XV12_MODEL_PATH=str(ROOT / "config" / "runtime.json"))
        result = run_ps(MODEL_SCRIPT, "Stop", env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert process.poll() is None
        assert not (state_dir / "model.json").exists()
    finally:
        terminate(process)


def test_optional_comfyui_unavailable_cannot_prevent_backend_stage():
    start = START_SCRIPT.read_text(encoding="utf-8")
    warning = start.index("OPTIONAL COMFYUI WARNING")
    backend = start.index("xv12-backend.ps1")
    assert "try {" in start[:warning] and warning < backend
    assert "continuing required core startup" in start


def test_model_startup_failure_is_required_and_reports_the_missing_executable(tmp_path: Path):
    env = lifecycle_env(tmp_path)
    env.update(XV12_MODEL_PORT=str(free_port()), XV12_MODEL_EXECUTABLE=str(tmp_path / "missing-llama-server.exe"))
    result = run_ps(MODEL_SCRIPT, "Ensure", env)
    assert result.returncode != 0
    assert "llama-server.exe is missing" in result.stdout + result.stderr


def test_backend_startup_failure_is_required_and_reports_the_missing_runtime(tmp_path: Path):
    env = lifecycle_env(tmp_path)
    env.update(XV12_APP_PORT=str(free_port()), XV12_BACKEND_PYTHON=str(tmp_path / "missing-python.exe"))
    result = run_ps(BACKEND_SCRIPT, "Ensure", env)
    assert result.returncode != 0
    assert "Python runtime is missing" in result.stdout + result.stderr


def test_foreign_model_port_reports_exact_pid_and_port(tmp_path: Path):
    port = free_port()
    process = fixture_server("model", port, ["foreign-process"])
    try:
        env = lifecycle_env(tmp_path)
        env.update(XV12_MODEL_PORT=str(port), XV12_MODEL_EXECUTABLE=sys._base_executable, XV12_MODEL_PATH=str(ROOT / "config" / "runtime.json"))
        result = run_ps(MODEL_SCRIPT, "Ensure", env)
        output = result.stdout + result.stderr
        assert result.returncode != 0 and str(port) in output and str(listener_pid(port)) in output
        assert "Foreign process conflict on model port" in output
    finally:
        terminate(process)


def test_foreign_backend_port_reports_exact_pid_and_port(tmp_path: Path):
    port = free_port()
    process = fixture_server("backend", port, ["foreign-process"])
    try:
        env = lifecycle_env(tmp_path)
        env.update(XV12_APP_PORT=str(port), XV12_BACKEND_PYTHON=sys.executable)
        result = run_ps(BACKEND_SCRIPT, "Ensure", env)
        output = result.stdout + result.stderr
        assert result.returncode != 0 and str(port) in output and str(listener_pid(port)) in output
        assert "Foreign process conflict on backend port" in output
    finally:
        terminate(process)


def test_health_readiness_waits_require_live_alias_context_and_application_contract():
    model = MODEL_SCRIPT.read_text(encoding="utf-8")
    backend = BACKEND_SCRIPT.read_text(encoding="utf-8")
    assert "function Wait-ModelReady" in model and "context_ok" in model and "meta.n_ctx" in model
    assert "function Wait-BackendReady" in backend and "contract_ok" in backend and "$payload.ok" in backend
    assert "Start-Sleep" in model and "Start-Sleep" in backend


def test_ui_launch_is_strictly_after_core_health_contract_and_before_final_ready():
    start = START_SCRIPT.read_text(encoding="utf-8")
    health = start.index("APPLICATION HEALTH CONTRACT PASSED")
    ui = start.index("UI LAUNCH: opening")
    ready = start.index("READY: XODUZ XV12")
    assert health < ui < ready


def test_backend_reuses_a_verified_healthy_listener_and_recovers_state(tmp_path: Path):
    port = free_port()
    identity = [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)]
    process = fixture_server("backend", port, identity)
    try:
        env = lifecycle_env(tmp_path)
        env.update(XV12_APP_PORT=str(port), XV12_BACKEND_PYTHON=sys.executable)
        result = run_ps(BACKEND_SCRIPT, "Ensure", env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "BACKEND STATE RECOVERY" in result.stdout and "BACKEND REUSE" in result.stdout
        state = json.loads((tmp_path / "state" / "backend.json").read_text(encoding="utf-8-sig"))
        assert state["listener_pid"] == listener_pid(port)
    finally:
        terminate(process)


def test_launcher_scripts_are_valid_windows_powershell_syntax():
    command = "; ".join(
        f"$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$t,[ref]$e)|Out-Null;if($e.Count){{throw $e[0]}}"
        for path in (COMMON_SCRIPT, MODEL_SCRIPT, BACKEND_SCRIPT, START_SCRIPT)
    )
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
