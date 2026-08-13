from __future__ import annotations

import uuid

import pytest

from .conftest import create_conversation, login


pytestmark = pytest.mark.assistant


class _AlwaysInvestigatingModel:
    """Never naturally completes -- always calls a tool while tools are offered, forcing the
    round-limit bound and a tools-disabled final synthesis, matching how a real bounded
    partial investigation would look."""

    async def stream_events(self, messages, tools=None):
        if tools:
            yield {"type": "tool_call", "id": str(uuid.uuid4()), "name": "engineering_repo_map", "arguments": '{"path": "X:\\\\XV12"}'}
        else:
            yield {"type": "content", "text": "I mapped part of the repository before the budget ran out."}


class _ContinuesFromEvidenceModel:
    def __init__(self) -> None:
        self.first_prompt_contained_evidence = False

    async def stream_events(self, messages, tools=None):
        prompt_text = " ".join(str(item.get("content") or "") for item in messages)
        if "engineering.repo.map" in prompt_text or "durable_evidence" in prompt_text or "engineering_repo_map" in prompt_text:
            self.first_prompt_contained_evidence = True
        yield {"type": "content", "text": "Continuing from where I left off using the prior evidence."}


def test_continue_reuses_evidence_from_a_partial_prior_turn(app, client):
    user = login(client, "admin")
    conversation = create_conversation(client)

    app.state.model = _AlwaysInvestigatingModel()
    first = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "Investigate the repository at X:\\XV12 in depth."},
    )
    assert first.status_code == 200
    stored_after_first = client.get(f"/api/conversations/{conversation['id']}").json()
    assert stored_after_first["messages"][-1]["status"] != "complete"

    evidence = app.state.store.get_evidence(user["id"], conversation["id"])
    assert evidence is not None
    assert "engineering.repo.map" in evidence["sources_inspected"]
    assert evidence["stop_reason"]

    assembled = app.state.context.assemble(user, conversation["id"])
    assert "durable_evidence" in assembled.sections
    assert "engineering.repo.map" in assembled.messages[0]["content"]

    continuation_model = _ContinuesFromEvidenceModel()
    app.state.model = continuation_model
    second = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Continue."})
    assert second.status_code == 200
    assert continuation_model.first_prompt_contained_evidence is True

    stored_after_second = client.get(f"/api/conversations/{conversation['id']}").json()
    assert stored_after_second["messages"][-1]["status"] == "complete"
    assert app.state.store.get_evidence(user["id"], conversation["id"]) is None
