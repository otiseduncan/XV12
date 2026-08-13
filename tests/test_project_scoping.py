from __future__ import annotations

import json
import uuid

import pytest

from .conftest import create_conversation, login


pytestmark = pytest.mark.project_context


class _RegistersProjectModel:
    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        self.registered = False

    async def stream_events(self, messages, tools=None):
        if not self.registered:
            self.registered = True
            yield {
                "type": "tool_call", "id": str(uuid.uuid4()), "name": "project_register",
                "arguments": json.dumps({"name": self.project_name, "description": f"Working on {self.project_name}"}),
            }
        else:
            yield {"type": "content", "text": f"Registered and activated {self.project_name} for this conversation."}


def test_two_simultaneous_conversations_carry_different_active_projects(app, client):
    user = login(client, "admin")
    conversation_atlas = create_conversation(client)
    conversation_borealis = create_conversation(client)

    app.state.model = _RegistersProjectModel("Atlas")
    response_atlas = client.post(f"/api/conversations/{conversation_atlas['id']}/stream", json={"message": "Register project Atlas."})
    assert response_atlas.status_code == 200
    assert "Atlas" in response_atlas.text

    app.state.model = _RegistersProjectModel("Borealis")
    response_borealis = client.post(f"/api/conversations/{conversation_borealis['id']}/stream", json={"message": "Register project Borealis."})
    assert response_borealis.status_code == 200
    assert "Borealis" in response_borealis.text

    atlas_context = app.state.context.assemble(user, conversation_atlas["id"])
    borealis_context = app.state.context.assemble(user, conversation_borealis["id"])

    assert "Atlas" in atlas_context.messages[0]["content"]
    assert "Borealis" not in atlas_context.messages[0]["content"]
    assert "Borealis" in borealis_context.messages[0]["content"]
    assert "Atlas" not in borealis_context.messages[0]["content"]

    projects = client.get("/api/projects").json()
    assert {item["name"] for item in projects} == {"Atlas", "Borealis"}


def test_activate_project_for_conversation_does_not_disturb_a_different_conversation(app, client):
    store = app.state.store
    user = login(client, "admin")
    conversation_one = create_conversation(client)
    conversation_two = create_conversation(client)
    project_a = store.create_project(user["id"], "Northwind", None)
    project_b = store.create_project(user["id"], "Southgate", None)

    store.activate_project_for_conversation(user["id"], conversation_one["id"], project_a["id"])
    store.activate_project_for_conversation(user["id"], conversation_two["id"], project_b["id"])

    assert store.active_project_for_conversation(user["id"], conversation_one["id"])["name"] == "Northwind"
    assert store.active_project_for_conversation(user["id"], conversation_two["id"])["name"] == "Southgate"

    store.deactivate_project_for_conversation(user["id"], conversation_one["id"])
    assert store.active_project_for_conversation(user["id"], conversation_one["id"]) is None
    assert store.active_project_for_conversation(user["id"], conversation_two["id"])["name"] == "Southgate"


def test_conversation_scoping_falls_back_to_global_last_selected_project(app, client):
    """A conversation with no explicit in-conversation activation still inherits the user's
    globally last-selected project (preserves the existing standalone Projects UI/chip)."""
    store = app.state.store
    user = login(client, "admin")
    conversation = create_conversation(client)
    project = store.create_project(user["id"], "GlobalDefault", None)
    store.activate_project(user["id"], project["id"])

    resolved = store.active_project_for_conversation(user["id"], conversation["id"])
    assert resolved is not None and resolved["name"] == "GlobalDefault"


def test_project_scoping_does_not_leak_across_users(app, client):
    from fastapi.testclient import TestClient

    with TestClient(app) as user_a_client, TestClient(app) as user_b_client:
        user_a = login(user_a_client, "user-a")
        login(user_b_client, "user-b")
        conversation_a = create_conversation(user_a_client)
        store = app.state.store
        project = store.create_project(user_a["id"], "PrivateToA", None)
        store.activate_project_for_conversation(user_a["id"], conversation_a["id"], project["id"])
        assert user_b_client.get("/api/projects").json() == []
