from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.builder_execution import BUILDER_MODEL_MAX_TOKENS, BuilderEvidence, BuilderExecutionService
from app.creator import CreatorStore
from app.model_compat import ToolCallCompatibilityModel
from .conftest import create_conversation, login


pytestmark = pytest.mark.creator
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 1200


def call(client: TestClient, capability_id: str, **arguments: Any) -> dict[str, Any]:
    response = client.post(f"/api/capabilities/{capability_id}", json={"arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def wait_job(client: TestClient, job_id: str, timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = client.get(f"/api/creator/jobs/{job_id}")
        assert current.status_code == 200, current.text
        body = current.json()
        if body["state"] in {"succeeded", "failed", "cancelled"}:
            return body
        time.sleep(0.03)
    raise AssertionError(f"Builder job {job_id} did not finish")


class ModelDirectedBuilder:
    def __init__(self) -> None:
        self.builder_round = 0
        self.builder_tool_calls = 0

    async def health(self) -> dict[str, Any]:
        return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

    async def complete(self, _messages: list[dict[str, Any]], max_tokens: int = 320) -> str:
        return '{"satisfied":true,"missing":[]}'

    async def stream_events(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        names = {str(item["function"]["name"]) for item in tools or []}
        if "builder_session_execute" in names:
            if any(item.get("role") == "tool" for item in messages):
                yield {"type": "content", "text": "I started a durable Builder session. The verified site will appear in this chat."}
            else:
                request = next(str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user")
                yield {"type": "tool_call", "id": "outer-builder", "name": "builder_session_execute",
                       "arguments": json.dumps({"request": request, "mode": "build", "title": "Sally's Seashells"})}
            return
        rounds: list[list[tuple[str, dict[str, Any]]]] = [
            [
                ("builder_files_batch", {"files": [
                    {"path": "index.html", "content": "<!doctype html><html><head><title>Sally's Seashells</title><link rel='stylesheet' href='styles.css'></head><body><main><h1>Sally's Seashells</h1><section id='featured'>Coastal collection</section><button id='shop'>Shop shells</button><script src='app.js'></script></main></body></html>"},
                    {"path": "styles.css", "content": "body{font-family:system-ui;background:#f7eee2;color:#183c46}main{max-width:960px;margin:auto;padding:48px}"},
                    {"path": "app.js", "content": "throw new Error('negative control');"},
                    {"path": "test_app.py", "content": "from pathlib import Path\ndef test_site(): assert \"Sally's Seashells\" in Path('index.html').read_text()\n"},
                ]}),
                ("builder_sandbox_exec", {"argv": ["python", "-m", "pytest", "-q"], "report_type": "test_report"}),
                ("builder_preview_start", {"title": "Sally's Seashells"}),
            ],
            [("browser_preview_inspect", {})],
            [
                ("builder_files_read", {"path": "app.js"}),
                ("builder_files_patch", {"path": "app.js", "content": "document.querySelector('#shop').onclick=()=>document.body.dataset.shopped='yes';"}),
                ("builder_sandbox_exec", {"argv": ["python", "-m", "pytest", "-q"], "report_type": "test_report"}),
            ],
            [("browser_preview_inspect", {"click_selector": "#shop"})],
        ]
        if self.builder_round < len(rounds):
            calls = rounds[self.builder_round]
            self.builder_round += 1
            for index, (name, arguments) in enumerate(calls):
                self.builder_tool_calls += 1
                yield {"type": "tool_call", "id": f"builder-{self.builder_round}-{index}",
                       "name": name, "arguments": json.dumps(arguments)}
        else:
            self.builder_round += 1
            yield {"type": "content", "text": "The tested and browser-validated application is ready."}


class ContinuationBuilder:
    def __init__(self) -> None:
        self.round = 0

    async def complete(self, _messages, max_tokens=320):
        return '{"satisfied":true,"missing":[]}'

    async def stream_events(self, _messages, tools=None):
        rounds = [
            [
                ("builder_files_patch", {"path": "styles.css", "content": "body{font-family:Georgia;background:#f7f5ef;color:#143d38}main{max-width:1060px;margin:auto;padding:64px}#featured{border-top:3px solid #58a99b}"}),
                ("builder_sandbox_exec", {"argv": ["python", "-m", "pytest", "-q"], "report_type": "test_report"}),
            ],
            [("browser_preview_inspect", {})],
        ]
        if self.round < len(rounds):
            calls = rounds[self.round]
            self.round += 1
            for index, (name, arguments) in enumerate(calls):
                yield {"type": "tool_call", "id": f"continue-{self.round}-{index}", "name": name,
                       "arguments": json.dumps(arguments)}
        else:
            self.round += 1
            yield {"type": "content", "text": "The upscale revision is validated."}


def install_portable_builder_handlers(app, *, browser_fail_first: bool = True) -> dict[str, Any]:
    platform = app.state.creator_platform
    state = {"browser_calls": 0}

    def sandbox(arguments, _user):
        return {"status": "success", "executed": True, "exit_code": 0,
                "summary": "portable test/build passed", "artifact": {"type": arguments.get("report_type", "test_report")}}

    def preview_start(arguments, user):
        preview_id = str(uuid.uuid4())
        workspace_id = str(arguments["workspace_id"])
        access_token = platform.store.set_preview(
            preview_id, user["id"], workspace_id, "portable-preview", 18499, "http://127.0.0.1:18499/"
        )
        root = platform.store.safe_path(workspace_id, user["id"], ".", must_exist=True)
        manifest = root / ".xv12-artifacts" / f"application-{preview_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"preview_id": preview_id}), encoding="utf-8")
        artifact = platform.artifacts.register_file(
            user_id=user["id"], capability_id="builder.preview.start", source_path=manifest,
            title=str(arguments.get("title") or "Application"), source_label="Portable Builder test",
            conversation_id=str(arguments["conversation_id"]), artifact_type="application", actions=["open"],
            metadata={"preview_url": platform.previews.proxy_url(preview_id, access_token), "preview_id": preview_id,
                      "workspace_id": workspace_id, "managed_preview": True},
        )
        return {"status": "success", "preview": {"id": preview_id, "state": "running", "workspace_id": workspace_id},
                "artifact": artifact}

    def preview_status(arguments, user):
        preview = platform.store.preview(str(arguments["preview_id"]), user["id"])
        return {"status": "success", "preview": {"id": preview["id"], "state": "running",
                "workspace_id": preview["workspace_id"]}} if preview else {"status": "no_result"}

    def browser(_arguments, _user):
        state["browser_calls"] += 1
        healthy = not browser_fail_first or state["browser_calls"] > 1
        return {"status": "success", "rendered": True, "healthy": healthy,
                "title": "Sally's Seashells", "runtime_errors": [] if healthy else ["negative control"],
                "network_failures": [], "console": [] if healthy else [{"level": "error", "text": "negative control"}]}

    def screenshot(arguments, user):
        preview = platform.store.preview(str(arguments["preview_id"]), user["id"])
        root = platform.store.safe_path(str(preview["workspace_id"]), user["id"], ".", must_exist=True)
        target = root / ".xv12-artifacts" / f"screenshot-{uuid.uuid4().hex}.png"
        target.write_bytes(PNG)
        artifact = platform.artifacts.register_file(
            user_id=user["id"], capability_id="browser.preview.screenshot", source_path=target,
            title="Verified application screenshot", source_label="Portable Chromium evidence",
            conversation_id=str(arguments["conversation_id"]), artifact_type="screenshot", actions=["view", "download"],
            metadata={"preview_id": preview["id"], "workspace_id": preview["workspace_id"]},
        )
        return {"status": "success", "rendered": True, "artifact": artifact}

    app.state.gateway.handlers["builder.sandbox.exec"] = sandbox
    app.state.gateway.handlers["builder.preview.start"] = preview_start
    app.state.gateway.handlers["builder.preview.status"] = preview_status
    app.state.gateway.handlers["browser.preview.inspect"] = browser
    app.state.gateway.handlers["browser.preview.screenshot"] = screenshot
    return state


def test_chat_uses_one_durable_builder_call_and_model_repairs_negative_control(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    model = ModelDirectedBuilder()
    app.state.model = model
    state = install_portable_builder_handlers(app)

    response = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "Generate a website for Sally's Seashells."},
    )
    assert response.status_code == 200
    assert "safe tool-call limit" not in response.text
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    cards = stored["messages"][-1]["metadata"]["capability_cards"]
    assert [card["capability_id"] for card in cards] == ["builder.session.execute"]
    queued = cards[0]["result"]
    completed = wait_job(client, queued["job"]["job_id"])
    assert completed["state"] == "succeeded", completed
    result = completed["result"]
    assert result["status"] == "success" and result["validation"]["healthy"] is True
    assert result["operations_completed"] > 4 and model.builder_tool_calls > 4
    assert result["builder_session"]["repair_cycles"] == 1 and state["browser_calls"] == 2
    assert result["artifact"]["type"] == "application"
    assert result["artifact"]["metadata"]["preview_url"].startswith("/api/creator/previews/")
    assert result["artifact"]["metadata"]["screenshot"]["type"] == "screenshot"
    assert result["artifact"]["metadata"]["project_archive"]["type"] == "project_archive"
    assert result["artifact"]["metadata"]["healthy"] is True


def test_ordinary_model_exposes_only_the_high_level_builder_capability(app, client):
    admin = login(client, "admin")
    tool_names = {item["function"]["name"] for item in app.state.registry.model_tools(admin)}
    assert "builder_session_execute" in tool_names
    assert "job_status" not in tool_names
    assert "job_cancel" in tool_names
    assert tool_names.isdisjoint({
        "builder_workspace_create", "builder_workspace_open", "builder_workspace_inspect",
        "builder_files_read", "builder_files_patch", "builder_files_batch", "builder_sandbox_exec",
        "builder_preview_start", "builder_preview_status", "builder_preview_stop",
        "browser_preview_inspect", "browser_preview_screenshot", "builder_project_archive",
    })


def test_builder_model_response_budget_is_scoped_and_does_not_mutate_normal_chat():
    class BaseModel:
        def __init__(self, settings):
            self.settings = settings

    ordinary_settings = SimpleNamespace(model_max_tokens=768)
    ordinary = ToolCallCompatibilityModel(BaseModel(ordinary_settings))
    service = object.__new__(BuilderExecutionService)
    service.model_provider = lambda: ordinary
    builder = service._model()
    assert ordinary.model.settings.model_max_tokens == 768
    assert builder.model.settings.model_max_tokens == BUILDER_MODEL_MAX_TOKENS


def test_file_changes_invalidate_prior_test_and_browser_evidence():
    evidence = BuilderEvidence(
        workspace_id="workspace", files_changed=True, sandbox_succeeded=True, browser_healthy=True,
    )
    BuilderExecutionService._observe("builder.files.patch", {"status": "success"}, evidence)
    assert evidence.files_changed is True
    assert evidence.sandbox_succeeded is False
    assert evidence.browser_healthy is False


def test_same_model_requirement_review_returns_concrete_missing_items():
    class Reviewer:
        reviewed_messages = None

        async def complete(self, _messages, max_tokens=512):
            self.reviewed_messages = _messages
            return '{"satisfied":false,"missing":["Featured collection section is absent"]}'

    reviewer = Reviewer()
    satisfied, missing = asyncio.run(BuilderExecutionService._review_requirements(
        reviewer, {"original_request": "Add a featured collection"},
        BuilderEvidence(workspace_id="workspace", browser_title="Store", browser_body_text="Our collection"),
        [],
    ))
    assert satisfied is False
    assert missing == ["Featured collection section is absent"]
    assert "must be present in the final Chromium-visible text" in reviewer.reviewed_messages[0]["content"]


def test_builder_continuation_reuses_workspace_preview_and_artifact(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    install_portable_builder_handlers(app, browser_fail_first=False)
    app.state.model = ModelDirectedBuilder()
    first = call(client, "builder.session.execute", request="Build a coastal store website", mode="build", title="Coastal store",
                 conversation_id=conversation["id"])
    first_job = wait_job(client, first["job"]["job_id"])
    assert first_job["state"] == "succeeded", first_job
    first_result = first_job["result"]

    app.state.model = ContinuationBuilder()
    second = call(
        client, "builder.session.execute",
        request="Make the site feel more upscale, add a featured collection section, and change the accent color to sea-glass green.",
        mode="build", conversation_id=conversation["id"],
    )
    second_job = wait_job(client, second["job"]["job_id"])
    assert second_job["state"] == "succeeded", second_job
    second_result = second_job["result"]
    assert second_result["workspace_id"] == first_result["workspace_id"]
    assert second_result["preview_id"] == first_result["preview_id"]
    assert second_result["artifact"]["id"] == first_result["artifact"]["id"]
    assert second_result["builder_session"]["parent_session_id"] == first_result["builder_session"]["id"]
    assert second_result["builder_session"]["mode"] == "modify"
    root = app.state.creator_platform.store.safe_path(second_result["workspace_id"], login(client, "admin")["id"], ".")
    assert "#58a99b" in (root / "styles.css").read_text(encoding="utf-8")


class EndlessBuilder:
    async def stream_events(self, _messages, tools=None):
        for index in range(2):
            yield {"type": "tool_call", "id": f"endless-{uuid.uuid4().hex}", "name": "builder_workspace_inspect",
                   "arguments": "{}"}


def test_builder_hard_bound_returns_persisted_partial_success(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    app.state.model = EndlessBuilder()
    queued = call(client, "builder.session.execute", request="Keep inspecting forever", mode="build",
                  conversation_id=conversation["id"])
    completed = wait_job(client, queued["job"]["job_id"])
    assert completed["state"] == "failed"
    result = completed["result"]
    assert result["status"] == "partial_success" and result["workspace_preserved"] is True
    assert result["operations_completed"] == 32
    assert "safe tool-call limit" not in result["message"]
    assert result["builder_session"]["status"] == "partial_success"


class SlowBuilder:
    async def stream_events(self, _messages, tools=None):
        await asyncio.sleep(0.08)
        yield {"type": "tool_call", "id": str(uuid.uuid4()), "name": "builder_workspace_inspect", "arguments": "{}"}


def test_builder_cancellation_preserves_workspace(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    app.state.model = SlowBuilder()
    queued = call(client, "builder.session.execute", request="Build something slowly", mode="build",
                  conversation_id=conversation["id"])
    cancelled = client.post(f"/api/creator/jobs/{queued['job']['job_id']}/cancel")
    assert cancelled.status_code == 200
    completed = wait_job(client, queued["job"]["job_id"])
    assert completed["state"] == "cancelled"
    assert completed["result"]["workspace_preserved"] is True
    assert app.state.creator_platform.store.workspace(completed["workspace_id"], login(client, "admin")["id"])


def test_builder_session_and_preview_are_cross_user_isolated(app):
    with TestClient(app) as admin_client, TestClient(app) as other_client:
        admin = login(admin_client, "admin")
        conversation = create_conversation(admin_client)
        session = app.state.creator_platform.store.create_builder_session(
            user_id=admin["id"], conversation_id=conversation["id"],
            workspace_id=app.state.creator_platform.store.create_workspace(admin["id"], "Private site")["id"],
            request="Private site", mode="build",
        )
        preview_id = str(uuid.uuid4())
        access_token = app.state.creator_platform.store.set_preview(
            preview_id, admin["id"], session["workspace_id"], "private-preview", 18498, "http://127.0.0.1:18498/"
        )
        job = app.state.creator_platform.store.create_job(
            admin["id"], conversation["id"], "builder.session.execute", session["workspace_id"], {"request": "Private"}
        )
        login(other_client, "user-a")
        assert other_client.get(f"/api/creator/builder-sessions/{session['id']}").status_code == 404
        assert other_client.get(f"/api/creator/jobs/{job['id']}").status_code == 404
        assert other_client.post(f"/api/creator/jobs/{job['id']}/cancel").status_code == 404
        assert other_client.get(f"/api/creator/previews/{preview_id}/index.html").status_code == 404
        assert other_client.get(f"/api/creator/previews/{preview_id}/token/not-a-valid-token/index.html").status_code == 404
        assert len(access_token) >= 32
        denied_open = other_client.post(
            "/api/capabilities/builder.workspace.open",
            json={"arguments": {"workspace_id": session["workspace_id"]}},
        )
        assert denied_open.status_code == 403
        denied = other_client.post(
            "/api/capabilities/builder.project.archive",
            json={"arguments": {"workspace_id": session["workspace_id"], "conversation_id": conversation["id"]}},
        )
        assert denied.status_code == 403


def test_owned_preview_proxy_is_same_origin_and_not_an_open_proxy(app, client):
    admin = login(client, "admin")
    workspace = app.state.creator_platform.store.create_workspace(admin["id"], "Proxy test")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "https://example.com/")
                self.end_headers()
                return
            body = b"<!doctype html><title>Owned preview</title><button>Safe</button>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    preview_id = str(uuid.uuid4())
    try:
        port = int(server.server_address[1])
        access_token = app.state.creator_platform.store.set_preview(
            preview_id, admin["id"], workspace["id"], "owned-preview", port, f"http://127.0.0.1:{port}/"
        )
        response = client.get(f"/api/creator/previews/{preview_id}/index.html")
        assert response.status_code == 200 and "Owned preview" in response.text
        root_response = client.get(f"/api/creator/previews/{preview_id}/")
        assert root_response.status_code == 200
        assert response.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
        assert client.get(f"/api/creator/previews/{preview_id}/redirect").status_code == 502
        assert client.get(f"/api/creator/previews/{preview_id}/../secret").status_code in {400, 404}
        assert client.get(f"/api/creator/previews/{uuid.uuid4()}/https://example.com").status_code == 404
        with TestClient(app) as sandbox_client:
            token_root = sandbox_client.get(f"/api/creator/previews/{preview_id}/token/{access_token}/")
            assert token_root.status_code == 200 and "Owned preview" in token_root.text
            assert sandbox_client.get(
                f"/api/creator/previews/{preview_id}/token/{'x' * 43}/"
            ).status_code == 404
            assert sandbox_client.get(
                f"/api/creator/previews/{preview_id}/token/{access_token}/redirect"
            ).status_code == 502
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_chat_renderer_uses_secure_live_application_contract():
    source = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'frame.setAttribute("sandbox", "allow-forms allow-modals allow-scripts")' in source
    assert "allow-same-origin" not in source
    assert 'project.textContent = "Download Project"' in source
    assert 'view.textContent = artifact.type === "application" ? "Open / Expand"' in source
    assert "application-fallback" in source and "metadata?.screenshot?.reference" in source


def test_builder_restart_reconciliation_is_truthful(app, client):
    admin = login(client, "admin")
    conversation = create_conversation(client)
    store = app.state.creator_platform.store
    workspace = store.create_workspace(admin["id"], "Restart project")
    session = store.create_builder_session(
        user_id=admin["id"], conversation_id=conversation["id"], workspace_id=workspace["id"],
        request="Build through restart", mode="build",
    )
    store.update_builder_session(str(session["id"]), status="running", stage="Writing files")
    CreatorStore(store.path, store.workspace_root).initialize()
    recovered = store.builder_session(str(session["id"]), admin["id"])
    assert recovered["status"] == "interrupted"
    assert store.workspace(workspace["id"], admin["id"])


@pytest.mark.x_core
def test_ordinary_chat_tool_bound_is_still_four_rounds(app, client):
    class LoopingModel:
        calls = 0

        async def stream_events(self, _messages, tools=None):
            self.calls += 1
            yield {"type": "tool_call", "id": str(uuid.uuid4()), "name": "system_health_read", "arguments": "{}"}

    login(client, "admin")
    conversation = create_conversation(client)
    model = LoopingModel()
    app.state.model = model
    response = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Loop tools"})
    assert model.calls == 4
    assert "I reached the safe tool-call limit before I could finish that request." in response.text
