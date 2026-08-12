from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.creator import CreatorStore, JobManager, PreviewService
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
    assert health["image_provider"] == "unavailable"
    assert health["design_provider"] == "xoduz-local-design"
    assert health["image_provider_status"]["status"] == "disabled"
    assert health["realistic_fallback_to_design"] is False
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
        argv=["sh", "-lc", "printf '%s' \"$BUILD_TOKEN\"; test -z \"$XV12_TEST_CREATOR_TOKEN\""], secret_refs=["build_token"],
        timeout_seconds=30, report_type="test_report", conversation_id=conversation["id"],
    )
    assert receipt["executed"] is True and secret not in json.dumps(receipt)
    assert receipt["artifact"]["metadata"]["secret_values_exposed"] is False
    full_log = client.get(receipt["artifact"]["reference"])
    assert full_log.status_code == 200 and secret not in full_log.text and "[REDACTED]" in full_log.text


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
    isolated = call(client, "builder.sandbox.exec", workspace_id=workspace["id"],
                    argv=["sh", "-lc", "test -f index.html && test ! -e /var/run/docker.sock && test ! -e /host && test ! -e /repo && test ! -e /other && test ! -e /sys/class/net/eth0"],
                    timeout_seconds=30, conversation_id=conversation["id"])
    assert isolated["exit_code"] == 0 and isolated["sandbox"]["workspace_only_mount"] is True
    networked = call(client, "builder.sandbox.exec", workspace_id=workspace["id"],
                    argv=["sh", "-lc", "test -e /sys/class/net/eth0"], network=True,
                    timeout_seconds=30, conversation_id=conversation["id"])
    assert networked["exit_code"] == 0 and networked["sandbox"]["network"] == "enabled"
    tested = call(client, "builder.sandbox.exec", workspace_id=workspace["id"], argv=["python", "-m", "unittest", "-v"], report_type="test_report", timeout_seconds=60, conversation_id=conversation["id"])
    assert tested["exit_code"] == 0 and tested["artifact"]["type"] == "test_report"
    preview = call(client, "builder.preview.start", workspace_id=workspace["id"], title="Customer scheduling application", conversation_id=conversation["id"])
    preview_id = preview["preview"]["id"]
    try:
        assert preview["artifact"]["type"] == "application"
        inspected = call(client, "browser.preview.inspect", preview_id=preview_id)
        assert inspected["rendered"] is True and inspected["title"] == "Customer Scheduling Console"
        assert inspected["console_inspected"] is True and inspected["network_inspected"] is True and inspected["healthy"] is True
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
    image = call(client, "media.image.generate", prompt="A cinematic electric service scheduling command center", provider="design", title="Scheduling concept", width=960, height=540, conversation_id=conversation["id"])
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


def test_job_cancellation_and_preview_restart_reconciliation(tmp_path: Path):
    store = CreatorStore(tmp_path / "creator.sqlite", tmp_path / "workspaces")
    store.initialize()
    manager = JobManager(store)

    def slow(_job_id, progress, cancelled):
        for index in range(100):
            if cancelled():
                return {}
            progress(index, "Synthetic slow work")
            time.sleep(0.02)
        return {"completed": True}

    job = manager.submit("user", "conversation", "synthetic.slow", "", {}, slow)
    store.cancel_job(job["job_id"], "user")
    deadline, current = time.monotonic() + 5, store.job(job["job_id"], "user")
    while current and current["state"] not in {"cancelled", "failed", "succeeded"} and time.monotonic() < deadline:
        time.sleep(0.05)
        current = store.job(job["job_id"], "user")
    assert current and current["state"] == "cancelled" and current["progress"] == 100
    store.set_preview("preview", "user", "workspace", "definitely-not-a-container", 18499, "http://127.0.0.1:18499/")
    reconciled = PreviewService(store, None).reconcile()
    assert reconciled == {"checked": 1, "marked_stopped": 1}
    assert store.preview("preview", "user")["state"] == "stopped"


def test_browser_devtools_reports_console_runtime_network_and_click_evidence(client):
    login(client, "admin")
    conversation = create_conversation(client)
    workspace = call(client, "builder.workspace.create", name="Browser diagnostics")["workspace"]
    call(client, "builder.files.batch", workspace_id=workspace["id"], files=[
        {"path": "index.html", "content": "<html><head><title>Browser Diagnostics</title></head><body><button id='change'>Change state</button><output id='state'>before</output><script src='app.js'></script></body></html>"},
        {"path": "app.js", "content": "console.error('creator console proof');fetch('/missing.json');document.querySelector('#change').onclick=()=>document.querySelector('#state').textContent='after';setTimeout(()=>{throw new Error('creator runtime proof')},50);"},
    ])
    preview = call(client, "builder.preview.start", workspace_id=workspace["id"], conversation_id=conversation["id"])
    try:
        inspected = call(client, "browser.preview.inspect", preview_id=preview["preview"]["id"], click_selector="#change")
        assert inspected["rendered"] is True and inspected["console_inspected"] is True and inspected["network_inspected"] is True
        assert inspected["click_performed"] is True and "after" in inspected["body_text"]
        assert any("console proof" in item["text"] for item in inspected["console"])
        assert any("runtime proof" in item for item in inspected["runtime_errors"])
        assert any(item.get("status") == 404 or "missing" in item.get("url", "") for item in inspected["network_failures"])
        assert inspected["healthy"] is False
        telemetry = inspected["style_telemetry"]
        assert "error" not in telemetry, telemetry
        assert telemetry["viewport"]["w"] > 0 and telemetry["viewport"]["h"] > 0
        selectors = {item["sel"] for item in telemetry["elements"]}
        assert "button#change" in selectors and "output#state" in selectors
        button = next(item for item in telemetry["elements"] if item["sel"] == "button#change")
        assert button["rect"]["w"] > 0 and button["rect"]["h"] > 0
        assert button["font"]["size"].endswith("px") and button["bg"].startswith("rgb")
    finally:
        call(client, "builder.preview.stop", preview_id=preview["preview"]["id"])


def test_git_status_diff_commit_push_and_fast_forward_pull(app, client, tmp_path: Path):
    login(client, "admin")
    conversation = create_conversation(client)
    workspace = call(client, "builder.workspace.create", name="Git acceptance")["workspace"]
    root = app.state.creator_platform.store.safe_path(workspace["id"], client.cookies.get("unused", "") or next(
        row["id"] for row in app.state.permission_store.list_users() if row["role"] == "admin"
    ), ".")
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "XV12 Acceptance"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "xv12@example.invalid"], check=True)
    call(client, "builder.files.patch", workspace_id=workspace["id"], path="README.md", content="# Git acceptance\n")
    assert call(client, "git.status", workspace_id=workspace["id"])["clean"] is False
    initialized = call(client, "git.commit", workspace_id=workspace["id"], message="Initialize acceptance project", conversation_id=conversation["id"])
    assert initialized["exit_code"] == 0 and initialized["artifact"]["type"] == "git_receipt"
    call(client, "builder.files.patch", workspace_id=workspace["id"], path="README.md", content="# Git acceptance\n\nTracked change.\n")
    assert "README.md" in call(client, "git.diff", workspace_id=workspace["id"])["diff"]
    committed = call(client, "git.commit", workspace_id=workspace["id"], message="Update acceptance project", conversation_id=conversation["id"])
    assert committed["exit_code"] == 0 and committed["artifact"]["type"] == "git_receipt"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", str(remote)], check=True)
    pushed = call(client, "git.push", workspace_id=workspace["id"], conversation_id=conversation["id"])
    assert pushed["exit_code"] == 0
    subprocess.run(["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    clone = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "XV12 Acceptance"], check=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "xv12@example.invalid"], check=True)
    (clone / "upstream.txt").write_text("fast forward\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "upstream.txt"], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "Add upstream proof"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "push", "origin", "main"], check=True, capture_output=True)
    pulled = call(client, "git.pull", workspace_id=workspace["id"], conversation_id=conversation["id"])
    assert pulled["exit_code"] == 0 and (root / "upstream.txt").read_text() == "fast forward\n"


def test_chat_builds_and_then_edits_the_same_application_workspace(app, client):
    class BuilderConversationModel:
        workspace_id = ""

        async def health(self):
            return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

        async def stream_events(self, messages, tools=None):
            prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            tool_messages = [item for item in messages if item["role"] == "tool"]
            current_tools = tool_messages[-3:]
            if "change" in prompt.casefold():
                if not current_tools:
                    yield {"type": "tool_call", "id": "edit", "name": "builder_files_patch", "arguments": json.dumps({
                        "workspace_id": self.workspace_id, "path": "styles.css", "content": "body{background:#021018;color:#fff;font-family:system-ui}main{max-width:980px;margin:6vh auto;padding:48px}button{background:#ff9f43;padding:14px;border:0;border-radius:12px}",
                    })}
                    yield {"type": "tool_call", "id": "retest", "name": "builder_sandbox_exec", "arguments": json.dumps({
                        "workspace_id": self.workspace_id, "argv": ["python", "-m", "unittest", "-v"], "report_type": "test_report", "conversation_id": conversation["id"],
                    })}
                else:
                    yield {"type": "content", "text": f"I updated and retested the existing application in workspace {self.workspace_id}."}
                return
            if not tool_messages:
                yield {"type": "tool_call", "id": "workspace", "name": "builder_workspace_create", "arguments": '{"name":"Chat scheduling application"}'}
            elif not self.workspace_id:
                created = json.loads(tool_messages[-1]["content"])
                self.workspace_id = created["workspace"]["id"]
                yield {"type": "tool_call", "id": "files", "name": "builder_files_batch", "arguments": json.dumps({
                    "workspace_id": self.workspace_id, "files": [
                        {"path": "index.html", "content": "<html><head><title>Chat Scheduling App</title><link rel='stylesheet' href='styles.css'></head><body><main><h1>Chat Scheduling App</h1><button>Schedule customer</button></main></body></html>"},
                        {"path": "styles.css", "content": "body{background:#07141a;color:#dff;font-family:system-ui}main{padding:40px}"},
                        {"path": "test_app.py", "content": "from pathlib import Path\nimport unittest\nclass T(unittest.TestCase):\n def test_title(self): self.assertIn('Chat Scheduling App',Path('index.html').read_text())\n"},
                    ],
                })}
                yield {"type": "tool_call", "id": "tests", "name": "builder_sandbox_exec", "arguments": json.dumps({
                    "workspace_id": self.workspace_id, "argv": ["python", "-m", "unittest", "-v"], "report_type": "test_report", "conversation_id": conversation["id"],
                })}
                yield {"type": "tool_call", "id": "preview", "name": "builder_preview_start", "arguments": json.dumps({
                    "workspace_id": self.workspace_id, "title": "Chat scheduling application", "conversation_id": conversation["id"],
                })}
            else:
                yield {"type": "content", "text": f"I built, tested, and started the application in workspace {self.workspace_id}."}

    login(client, "admin")
    conversation = create_conversation(client)
    model = BuilderConversationModel()
    app.state.model = model
    response = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Build me a customer scheduling web application."})
    assert response.status_code == 200 and "event: error" not in response.text
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    cards = stored["messages"][-1]["metadata"]["capability_cards"]
    assert [card["capability_id"] for card in cards] == ["builder.workspace.create", "builder.files.batch", "builder.sandbox.exec", "builder.preview.start"]
    preview_id = cards[-1]["result"]["preview"]["id"]
    assert cards[-1]["result"]["artifact"]["type"] == "application"
    try:
        follow_up = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Change the accent to orange and rerun the tests."})
        assert follow_up.status_code == 200 and "event: error" not in follow_up.text
        stored = client.get(f"/api/conversations/{conversation['id']}").json()
        edit_cards = stored["messages"][-1]["metadata"]["capability_cards"]
        assert [card["capability_id"] for card in edit_cards] == ["builder.files.patch", "builder.sandbox.exec"]
        assert edit_cards[-1]["result"]["exit_code"] == 0
        assert model.workspace_id in stored["messages"][-1]["content"]
    finally:
        call(client, "builder.preview.stop", preview_id=preview_id)
