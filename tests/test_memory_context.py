from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from .conftest import create_conversation, login


@pytest.mark.memory_isolation
def test_conversations_are_strictly_user_scoped(app):
    with TestClient(app) as user_a, TestClient(app) as user_b:
        login(user_a, "user-a")
        private = create_conversation(user_a)
        user_a.post(f"/api/conversations/{private['id']}/stream", json={"message": "My private project is Atlas."}).read()
        login(user_b, "user-b")
        assert user_b.get(f"/api/conversations/{private['id']}").status_code == 404
        assert all(item["id"] != private["id"] for item in user_b.get("/api/conversations").json())


@pytest.mark.memory_isolation
def test_attachment_metadata_and_binding_cannot_cross_users(app):
    with TestClient(app) as user_a, TestClient(app) as user_b:
        login(user_a, "user-a")
        upload = user_a.post("/api/attachments", files={"file": ("private.txt", io.BytesIO(b"private"), "text/plain")})
        assert upload.status_code == 201
        attachment_id = upload.json()["id"]
        login(user_b, "user-b")
        conversation_b = create_conversation(user_b)
        response = user_b.post(f"/api/conversations/{conversation_b['id']}/stream", json={"message": "Use it", "attachment_ids": [attachment_id]})
        assert response.status_code == 404


@pytest.mark.context
def test_context_priorities_protect_identity_user_subject_and_recent(client, app):
    user = login(client, "user-a")
    conversation = create_conversation(client)
    app.state.store.add_message(user["id"], conversation["id"], "user", "Project Atlas is the active subject.")
    app.state.store.ensure_active_subject(user["id"], conversation["id"], "Project Atlas is the active subject.")
    assembled = app.state.context.assemble(user, conversation["id"])
    assert assembled.sections[:3] == ["identity", "authenticated_user", "active_subject"]
    assert assembled.estimated_tokens < 32768
    assert assembled.messages[0]["role"] == "system"
    assert "XODUZ" in assembled.messages[0]["content"]


@pytest.mark.context
def test_context_budget_reserves_space_for_tool_schemas(client, app):
    user = login(client, "admin")
    conversation = create_conversation(client)
    app.state.store.add_message(user["id"], conversation["id"], "user", "Short message.")
    from app.context import ContextAssembler

    with_tools = app.state.context.assemble(user, conversation["id"])
    without_tools = ContextAssembler(app.state.store, app.state.context.context_limit, registry=None).assemble(user, conversation["id"])
    assert with_tools.estimated_tokens <= without_tools.estimated_tokens
    assert app.state.context.registry is not None


@pytest.mark.context
def test_oversized_newest_message_does_not_silently_exceed_the_budget(client, app):
    from app.context import ContextAssembler, estimate_tokens

    user = login(client, "admin")
    conversation = create_conversation(client)
    tiny_context = ContextAssembler(app.state.store, 2000, registry=None)
    huge_text = "x" * 40000  # far larger than any plausible remaining budget at a 2000-token context limit
    app.state.store.add_message(user["id"], conversation["id"], "user", huge_text)
    assembled = tiny_context.assemble(user, conversation["id"])
    assert "recent_conversation" not in assembled.sections
    assert estimate_tokens(huge_text) > tiny_context.context_limit


@pytest.mark.context
def test_rolling_summary_compacts_older_turns(client, app):
    user = login(client)
    conversation = create_conversation(client)
    for index in range(20):
        role = "user" if index % 2 == 0 else "assistant"
        app.state.store.add_message(user["id"], conversation["id"], role, f"Project Atlas detail {index}: preserve this decision.")
    import asyncio
    assert asyncio.run(app.state.context.compact_if_needed(app.state.model, user, conversation["id"]))
    assembled = app.state.context.assemble(user, conversation["id"])
    assert assembled.summary_used
    assert "rolling_summary" in assembled.sections
