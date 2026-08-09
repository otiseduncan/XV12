from __future__ import annotations

import json
import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT
import app.web_tools as web_tools
from .conftest import create_conversation, login


@pytest.mark.user_identity
def test_conversational_identity_is_trusted_and_not_email_derived(app):
    with TestClient(app) as admin_client, TestClient(app) as user_client:
        admin = login(admin_client, "admin")
        user = login(user_client, "user-a")
        assert admin["conversational_name"] == "Otis"
        assert user["conversational_name"] == "Avery"
        assert "@" not in user["conversational_name"]
        changed = admin_client.patch(f"/api/admin/users/{user['id']}/preferred-name", json={"preferred_name": "Aves"})
        assert changed.status_code == 200 and changed.json()["conversational_name"] == "Aves"
        assert user_client.patch(f"/api/admin/users/{user['id']}/preferred-name", json={"preferred_name": "Nope"}).status_code == 403


@pytest.mark.project_context
@pytest.mark.memory_isolation
def test_projects_are_optional_active_and_user_scoped(app):
    with TestClient(app) as user_a, TestClient(app) as user_b:
        a = login(user_a, "user-a")
        project = user_a.post("/api/projects", json={"name": "Northstar", "reference": r"X:\Northstar", "description": "Calm service assistant"}).json()
        assert user_a.post(f"/api/projects/{project['id']}/activate").status_code == 200
        conversation = create_conversation(user_a)
        assembled = app.state.context.assemble(a, conversation["id"])
        assert "active_project" in assembled.sections
        assert "Northstar" in assembled.messages[0]["content"]
        login(user_b, "user-b")
        assert user_b.post(f"/api/projects/{project['id']}/activate").status_code == 404
        assert user_b.get("/api/projects").json() == []
        assert user_a.delete("/api/projects/active").status_code == 204


@pytest.mark.capability_registry
@pytest.mark.authorization
def test_service_start_is_admin_only_and_fixed_in_registry(client):
    login(client, "user-a")
    listing = client.get("/api/capabilities").json()["capabilities"]
    assert "service.calibration_iq.start" not in {item["id"] for item in listing}
    assert client.post("/api/capabilities/service.calibration_iq.start", json={"arguments": {}}).status_code == 403
    registry = json.loads((ROOT / "config" / "capabilities.v1.json").read_text(encoding="utf-8"))
    start = next(item for item in registry["capabilities"] if item["id"] == "service.calibration_iq.start")
    assert start["risk_tier"] == 2 and start["authorization"]["roles"] == ["admin"]
    assert start["arguments_schema"]["additionalProperties"] is False


@pytest.mark.databases
def test_adas_adapter_returns_only_verified_owned_data(client):
    login(client)
    coverage = client.post("/api/capabilities/adas.coverage.read", json={"arguments": {}}).json()["result"]
    assert coverage["status"] == "success" and coverage["domain_status"] == "verified"
    assert coverage["coverage"]["documents"] == 76
    assert coverage["coverage"]["verified_records"] == 6
    assert coverage["applications"][0]["make"] == "Hyundai"
    result = client.post(
        "/api/capabilities/adas.knowledge.search",
        json={"arguments": {"query": "2023 Hyundai Palisade front camera windshield replacement"}},
    ).json()["result"]
    assert result["status"] == "success" and result["domain_status"] == "verified"
    assert result["results"][0]["procedure"]["title"] == "Front Camera Adjustment (SPTAC or SPC)"
    assert result["evidence"]["verified_only"] is True
    miss = client.post(
        "/api/capabilities/adas.knowledge.search",
        json={"arguments": {"query": "2024 Rivian R1S front camera calibration"}},
    ).json()["result"]
    assert miss["status"] == "no_result" and miss["results"] == []


@pytest.mark.capability_registry
@pytest.mark.chat_core
def test_model_directed_tool_call_streams_card_and_persists_metadata(client, app):
    class ToolCallingModel:
        calls = 0

        async def stream_events(self, messages, tools=None) -> AsyncIterator[dict]:
            self.calls += 1
            assert tools and any(item["function"]["name"] == "adas_coverage_read" for item in tools)
            if not any(item["role"] == "tool" for item in messages):
                yield {"type": "tool_call", "id": "call-1", "name": "adas_coverage_read", "arguments": "{}"}
            else:
                yield {"type": "content", "text": "I found 76 ADAS documents and 6 verified records."}

        async def stream(self, messages):
            yield "unused"

        async def complete(self, messages, max_tokens=320):
            return "summary"

        async def health(self):
            return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

    login(client)
    app.state.model = ToolCallingModel()
    conversation = create_conversation(client)
    body = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Show ADAS coverage"}).text
    assert "event: capability" in body and "adas.coverage.read" in body
    assert "76 ADAS documents" in body
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    cards = stored["messages"][-1]["metadata"]["capability_cards"]
    assert cards[0]["capability_id"] == "adas.coverage.read"


@pytest.mark.ui_shell
def test_permanent_shell_has_internal_scroll_anchor_and_smart_scroll_controls():
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'class="avatar-panel"' in html and 'id="jump-latest"' in html
    assert ".messages {" in css and "overflow-y:auto" in css
    assert ".composer-wrap { flex:0 0 auto" in css
    assert "@media (max-width:760px)" in css
    assert ".sidebar { position:fixed" in css
    assert ".avatar-panel { min-height:118px" in css
    assert "function isNearBottom()" in js and "state.pinnedToBottom" in js


@pytest.mark.voice
def test_voice_is_single_session_cancelable_and_never_auto_restarts():
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    onend = js.split("recognition.onend =", 1)[1].split("};", 1)[0]
    assert "recognition.start" not in onend
    assert "recognition.abort" in js and "interimResults = true" in js


@pytest.mark.attachments
def test_pending_attachment_can_be_removed_before_send(client):
    import io

    login(client)
    item = client.post("/api/attachments", files={"file": ("remove-me.txt", io.BytesIO(b"context"), "text/plain")}).json()
    assert client.delete(f"/api/attachments/{item['id']}").status_code == 204
    assert client.delete(f"/api/attachments/{item['id']}").status_code == 404


@pytest.mark.web
def test_web_provider_returns_bounded_structured_current_evidence(monkeypatch):
    rss = """<rss><channel><item><title>Current event</title><link>https://example.test/story</link><description>Fresh evidence</description><pubDate>Sun, 09 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"""

    class Response:
        status_code = 200
        text = rss

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params=None):
            return Response()

    monkeypatch.setattr(web_tools.httpx, "AsyncClient", Client)
    result = asyncio.run(web_tools.current_search({"query": "current event", "mode": "news", "limit": 3}))
    assert result["status"] == "verified_results"
    assert result["executed_at"] and result["provider"] == "Bing News RSS"
    assert result["results"] == [{"title": "Current event", "url": "https://example.test/story", "snippet": "Fresh evidence", "published_at": "Sun, 09 Aug 2026 10:00:00 GMT", "source": "Bing News RSS", "reference": "web:1"}]
