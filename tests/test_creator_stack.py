from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.creator import CreatorStore
from .conftest import create_conversation, login


pytestmark = pytest.mark.creator


def call(client, capability_id: str, **arguments):
    response = client.post(f"/api/capabilities/{capability_id}", json={"arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_creator_registry_permissions_and_health_are_truthful(client):
    login(client, "admin")
    listing = client.get("/api/capabilities").json()
    ids = {item["id"] for item in listing["capabilities"]}
    required = {
        "job.status", "job.cancel", "builder.workspace.create", "builder.workspace.open",
        "builder.workspace.inspect", "builder.files.read", "builder.files.patch", "builder.files.batch",
        "builder.sandbox.exec", "builder.preview.start", "builder.preview.status", "builder.preview.stop",
        "browser.preview.inspect", "browser.preview.screenshot", "builder.project.archive",
        "git.status", "git.diff", "git.commit", "git.pull", "git.push",
        "secrets.reference.configure", "secrets.reference.status", "media.image.generate",
        "media.image.edit", "media.video.generate",
    }
    assert required <= ids
    health = client.get("/api/health").json()["services"]["creator"]
    assert health["job_manager"] == "available" and health["sandbox"] == "available"
    assert health["image_provider"] == "xoduz-local-design"
    assert health["secret_values_exposed"] is False


def test_workspaces_are_user_scoped_paths_are_bounded_and_batches_are_atomic(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as first, TestClient(app) as second:
        login(first, "admin")
        other = login(second, "user-a")
        workspace = call(first, "builder.workspace.create", name="Bounded application")["workspace"]
        result = call(first, "builder.files.batch", workspace_id=workspace["id"], files=[
            {"path": "src/app.js", "content": "console.log('safe');\n"},
            {"path": "README.md", "content": "# Bounded application\n"},
        ])
        assert result["atomic"] is True and result["files_written"] == 2
        read = call(first, "builder.files.read", workspace_id=workspace["id"], path="src/app.js")
        assert "safe" in read["content"] and len(read["sha256"]) == 64
        escape = call(first, "builder.files.patch", workspace_id=workspace["id"], path="../escape.txt", content="blocked")
        assert escape["status"] == "invalid_arguments"
        denied = second.post(f"/api/capabilities/builder.workspace.open", json={"arguments": {"workspace_id": workspace["id"]}})
        assert denied.status_code == 403  # no Builder grant
        app.state.permission_store.replace_grants(other["id"], {"builder": {"read"}}, "test-admin")
        missing = call(second, "builder.workspace.open", workspace_id=workspace["id"])
        assert missing["status"] == "no_result"


def test_secret_references_never_disclose_values_and_sandbox_redacts(app, client, monkeypatch):
    login(client, "admin")
    conversation = create_conversation(client)
    secret = "XV12_CREATOR_SECRET_DO_NOT_LEAK"
    monkeypatch.setenv("XV12_TEST_CREATOR_TOKEN", secret)
    configured = call(client, "secrets.reference.configure", name="build_token", environment_name="XV12_TEST_CREATOR_TOKEN", contexts=["builder"])
    assert configured["reference"]["configured"] is True
    assert secret not in json.dumps(configured) and "XV12_TEST_CREATOR_TOKEN" not in json.dumps(configured)
    status = call(client, "secrets.reference.status", name="build_token")
    assert status["configured"] is True and secret not in json.dumps(status)
    workspace = call(client, "builder.workspace.create", name="Secret boundary")["workspace"]
    receipt = call(
        client, "builder.sandbox.exec", workspace_id=workspace["id"],
        argv=["sh", "-lc", "printf '%s' \"$BUILD_TOKEN\""], secret_refs=["build_token"],
        timeout_seconds=30, report_type="test_report", conversation_id=conversation["id"],
    )
    assert receipt["executed"] is True and secret not in json.dumps(receipt)
    assert receipt["artifact"]["metadata"]["secret_values_exposed"] is False


def test_actual_builder_application_sandbox_preview_browser_and_archive(client):
    login(client, "admin")
    conversation = create_conversation(client)
    workspace = call(client, "builder.workspace.create", name="Customer scheduling console")["workspace"]
    files = [
        {"path": "index.html", "content": """<!doctype html><html><head><meta charset='utf-8'><title>Customer Scheduling Console</title><link rel='stylesheet' href='styles.css'></head><body><main><h1>Customer Scheduling Console</h1><p>Book, assign, and track service appointments.</p><section id='schedule'><button id='book'>Book appointment</button><output id='result'>Ready</output></section></main><script src='app.js'></script></body></html>"""},
        {"path": "styles.css", "content": "body{margin:0;background:#061318;color:#dff;font-family:system-ui}main{max-width:900px;margin:8vh auto;padding:40px}section{padding:24px;border:1px solid #23616a;border-radius:18px}button{padding:12px 18px;background:#41d5df;border:0;border-radius:9px}"},
        {"path": "app.js", "content": "document.querySelector('#book').addEventListener('click',()=>document.querySelector('#result').textContent='Appointment scheduled');"},
        {"path": "test_app.py", "content": "from pathlib import Path\nimport unittest\nclass AppTest(unittest.TestCase):\n def test_contract(self):\n  text=Path('index.html').read_text()\n  self.assertIn('Customer Scheduling Console',text)\n  self.assertIn('Book appointment',text)\nif __name__=='__main__': unittest.main()\n"},
    ]
    assert call(client, "builder.files.batch", workspace_id=workspace["id"], files=files)["files_written"] == 4
    tested = call(client, "builder.sandbox.exec", workspace_id=workspace["id"], argv=["python", "-m", "unittest", "-v"], report_type="test_report", timeout_seconds=60, conversation_id=conversation["id"])
    assert tested["exit_code"] == 0 and tested["artifact"]["type"] == "test_report"
    preview = call(client, "builder.preview.start", workspace_id=workspace["id"], title="Customer scheduling application", conversation_id=conversation["id"])
    preview_id = preview["preview"]["id"]
    try:
        assert preview["artifact"]["type"] == "application"
        inspected = call(client, "browser.preview.inspect", preview_id=preview_id)
        assert inspected["rendered"] is True and inspected["title"] == "Customer Scheduling Console"
        screenshot = call(client, "browser.preview.screenshot", preview_id=preview_id, conversation_id=conversation["id"])
        assert screenshot["rendered"] is True and screenshot["artifact"]["type"] == "screenshot"
        archive = call(client, "builder.project.archive", workspace_id=workspace["id"], conversation_id=conversation["id"])
        assert archive["artifact"]["type"] == "project_archive" and archive["artifact"]["downloadable"] is True
    finally:
        stopped = call(client, "builder.preview.stop", preview_id=preview_id)
        assert stopped["stopped"] is True


def test_image_edit_video_job_and_parent_artifact_continuity(client):
    login(client, "admin")
    conversation = create_conversation(client)
    image = call(client, "media.image.generate", prompt="A cinematic electric service scheduling command center", title="Scheduling concept", width=960, height=540, conversation_id=conversation["id"])
    assert image["actual_generation"] is True and image["artifact"]["type"] == "image"
    edited = call(client, "media.image.edit", source_artifact_id=image["artifact"]["id"], prompt="Add luminous teal status details", conversation_id=conversation["id"])
    assert edited["artifact"]["parent_artifact_id"] == image["artifact"]["id"]
    queued = call(client, "media.video.generate", source_artifact_id=edited["artifact"]["id"], prompt="Slow cinematic push in", duration_seconds=2, conversation_id=conversation["id"])
    assert queued["queued"] is True and queued["job"]["state"] in {"queued", "running"}
    deadline, current = time.monotonic() + 90, queued["job"]
    while time.monotonic() < deadline and current["state"] not in {"succeeded", "failed", "cancelled"}:
        time.sleep(0.5)
        current = client.get(f"/api/creator/jobs/{current['job_id']}").json()
    assert current["state"] == "succeeded", current
    video = current["result"]["artifact"]
    assert video["type"] == "video" and video["mime_type"] == "video/mp4"
    assert video["parent_artifact_id"] == edited["artifact"]["id"] and video["metadata"]["playable"] is True


def test_job_restart_reconciliation_is_persisted(tmp_path: Path):
    store = CreatorStore(tmp_path / "creator.sqlite", tmp_path / "workspaces")
    store.initialize()
    queued = store.create_job("user", "conversation", "synthetic.slow", "", {"delay": 10})
    store.update_job(queued["id"], state="running", progress=40, started_at="now")
    CreatorStore(tmp_path / "creator.sqlite", tmp_path / "workspaces").initialize()
    recovered = store.job(queued["id"], "user")
    assert recovered and recovered["state"] == "failed" and recovered["error_code"] == "service_restarted"
