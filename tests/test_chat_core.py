from __future__ import annotations

import pytest

from .conftest import create_conversation, login


@pytest.mark.chat_core
def test_natural_chat_stream_uses_model_and_persists(client, app):
    login(client)
    conversation = create_conversation(client)
    with client.stream("POST", f"/api/conversations/{conversation['id']}/stream", json={"message": "Good morning X.", "attachment_ids": []}) as response:
        body = response.read().decode()
    assert response.status_code == 200
    assert "event: delta" in body
    assert "I'm XODUZ" in body
    assert app.state.model.requests
    model_messages = app.state.model.requests[-1]
    assert model_messages[0]["role"] == "system"
    assert "You are XODUZ" in model_messages[0]["content"]
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    assert [item["role"] for item in stored["messages"]] == ["user", "assistant"]
    assert stored["messages"][1]["status"] == "complete"


@pytest.mark.chat_core
def test_conversation_continuity_includes_recent_subject(client, app):
    login(client)
    conversation = create_conversation(client)
    first = "I'm working on Project Atlas, a quiet desktop assistant."
    client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": first}).read()
    client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "What did I say it was?"}).read()
    latest = app.state.model.requests[-1]
    assert any(first in item["content"] for item in latest)
    assert "Project Atlas" in latest[0]["content"]


@pytest.mark.chat_core
def test_model_failure_records_partial_response_without_fake_success(client, app):
    class PartialFailureModel:
        async def stream(self, messages):
            yield "A partial response"
            raise RuntimeError("simulated model disconnect")

        async def complete(self, messages, max_tokens=320):
            return "summary"

        async def health(self):
            return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

    login(client)
    conversation = create_conversation(client)
    app.state.model = PartialFailureModel()
    body = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Start a response"}).text
    assert "event: error" in body
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    assert stored["messages"][-1]["content"] == "A partial response"
    assert stored["messages"][-1]["status"] == "failed"
