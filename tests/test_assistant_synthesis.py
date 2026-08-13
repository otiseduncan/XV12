from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.assistant import (
    ASSISTANT_DUPLICATE_EXECUTION_LIMIT,
    ASSISTANT_HARD_OPERATION_LIMIT,
    ASSISTANT_MODEL_ROUND_LIMIT,
    AssistantOrchestrator,
)
from .conftest import create_conversation, login


pytestmark = pytest.mark.assistant

ADMIN = {"id": "admin-1", "role": "admin", "status": "active"}


def base_messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are XODUZ."},
        {"role": "user", "content": "Investigate this for me."},
    ]


async def drain(orchestrator: AssistantOrchestrator, messages, user=ADMIN, **kwargs) -> list[dict[str, Any]]:
    return [event async for event in orchestrator.stream(messages, user, **kwargs)]


def final_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(event for event in reversed(events) if event["type"] == "complete")


def content_text(events: list[dict[str, Any]]) -> str:
    return "".join(str(event.get("text") or "") for event in events if event["type"] == "content")


# --- A. Terminal synthesis ---

def test_final_round_gets_a_tools_disabled_synthesis_call_grounded_in_evidence(app):
    class AlwaysCallingModel:
        rounds = 0
        synthesis_tools_arg = "unset"

        async def stream_events(self, messages, tools=None):
            self.rounds += 1
            if tools:
                yield {"type": "tool_call", "id": f"c{self.rounds}", "name": "system_health_read", "arguments": "{}"}
            else:
                self.synthesis_tools_arg = tools
                yield {"type": "content", "text": "Based on the health check evidence gathered, the system is reachable."}

    model = AlwaysCallingModel()
    orchestrator = AssistantOrchestrator(model, app.state.registry, app.state.gateway)
    events = asyncio.run(drain(orchestrator, base_messages()))
    final = final_event(events)
    assert final["status"] != "complete"
    assert "health check evidence" in content_text(events)
    assert not model.synthesis_tools_arg


# --- B. Independent operation ceiling ---

def test_operation_budget_is_independent_of_model_round_count(app):
    executed = {"count": 0}
    real_handler = app.state.gateway.handlers["system.health.read"]

    def counting_handler(arguments):
        executed["count"] += 1
        return real_handler(arguments)

    app.state.gateway.handlers["system.health.read"] = counting_handler

    class ManyCallsInOneRoundModel:
        served_synthesis = False

        async def stream_events(self, messages, tools=None):
            if not tools:
                self.served_synthesis = True
                yield {"type": "content", "text": "Stopped after the operation budget was reached."}
                return
            for index in range(ASSISTANT_HARD_OPERATION_LIMIT + 15):
                yield {"type": "tool_call", "id": f"c{index}", "name": "system_health_read", "arguments": "{}"}

    model = ManyCallsInOneRoundModel()
    orchestrator = AssistantOrchestrator(model, app.state.registry, app.state.gateway)
    events = asyncio.run(drain(orchestrator, base_messages()))
    assert executed["count"] <= ASSISTANT_HARD_OPERATION_LIMIT
    assert model.served_synthesis is True
    final = final_event(events)
    assert final["status"] != "complete"


# --- C. Duplicate-call suppression ---

def test_identical_repeated_calls_are_suppressed_after_a_bound(app):
    executed = {"count": 0}
    real_handler = app.state.gateway.handlers["system.health.read"]

    def counting_handler(arguments):
        executed["count"] += 1
        return real_handler(arguments)

    app.state.gateway.handlers["system.health.read"] = counting_handler

    class RepeatingModel:
        rounds = 0

        async def stream_events(self, messages, tools=None):
            self.rounds += 1
            if not tools or self.rounds > 6:
                yield {"type": "content", "text": "Done repeating."}
                return
            yield {"type": "tool_call", "id": f"c{self.rounds}", "name": "system_health_read", "arguments": "{}"}

    model = RepeatingModel()
    orchestrator = AssistantOrchestrator(model, app.state.registry, app.state.gateway)
    asyncio.run(drain(orchestrator, base_messages()))
    assert executed["count"] <= ASSISTANT_DUPLICATE_EXECUTION_LIMIT + 1


# --- D. Wall-time boundary ---

def test_wall_time_limit_stops_the_round_loop(app):
    class SlowLoopingModel:
        rounds = 0

        async def stream_events(self, messages, tools=None):
            self.rounds += 1
            if not tools:
                yield {"type": "content", "text": "Stopped by the wall-time bound."}
                return
            await asyncio.sleep(0.03)
            yield {"type": "tool_call", "id": f"c{self.rounds}", "name": "system_health_read", "arguments": "{}"}

    model = SlowLoopingModel()
    orchestrator = AssistantOrchestrator(model, app.state.registry, app.state.gateway)
    started = time.monotonic()
    events = asyncio.run(drain(orchestrator, base_messages(), wall_time_seconds=0.05))
    elapsed = time.monotonic() - started
    assert model.rounds < ASSISTANT_MODEL_ROUND_LIMIT
    assert elapsed < 5
    final = final_event(events)
    assert final["stop_reason"] == "wall_time_limit"


# --- E. Proper terminal state persisted (not ordinary "complete") ---

def test_budget_bound_turn_is_not_persisted_as_ordinary_complete(app, client):
    class AlwaysCallingModel:
        async def stream_events(self, messages, tools=None):
            if tools:
                yield {"type": "tool_call", "id": str(uuid.uuid4()), "name": "system_health_read", "arguments": "{}"}
            else:
                yield {"type": "content", "text": "Here is what I found before the budget ran out."}

    login(client, "admin")
    conversation = create_conversation(client)
    app.state.model = AlwaysCallingModel()
    response = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Loop tools forever"})
    assert response.status_code == 200
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    assistant_message = stored["messages"][-1]
    assert assistant_message["status"] != "complete"
    assert assistant_message["status"] in {"partial_success", "budget_exhausted"}
    assert assistant_message["metadata"]["stop_reason"] == "model_round_limit"
    assert assistant_message["metadata"]["final_synthesis_performed"] is True
    assert "model_rounds" in assistant_message["metadata"]
    assert "operation_count" in assistant_message["metadata"]


# --- F. Partial evidence response instructs the model what to summarize ---

def test_bounded_synthesis_prompt_asks_for_inspected_found_uncertain_and_why_stopped(app):
    class AlwaysCallingModel:
        synthesis_messages = None

        async def stream_events(self, messages, tools=None):
            if tools:
                yield {"type": "tool_call", "id": str(uuid.uuid4()), "name": "system_health_read", "arguments": "{}"}
            else:
                self.synthesis_messages = messages
                yield {"type": "content", "text": "Summary of partial findings."}

    model = AlwaysCallingModel()
    orchestrator = AssistantOrchestrator(model, app.state.registry, app.state.gateway)
    asyncio.run(drain(orchestrator, base_messages()))
    prompt_text = json.dumps(model.synthesis_messages)
    for phrase in ("inspected", "found", "uncertain", "stopped"):
        assert phrase in prompt_text.casefold()


# --- G. Pre-tool claim truthfulness ---

def test_same_round_content_before_a_tool_call_is_not_streamed_to_the_user(app):
    class ClaimThenCallModel:
        rounds = 0

        async def stream_events(self, messages, tools=None):
            self.rounds += 1
            if self.rounds == 1:
                yield {"type": "content", "text": "There are no issues at all."}
                yield {"type": "tool_call", "id": "c1", "name": "system_health_read", "arguments": "{}"}
            elif tools:
                yield {"type": "content", "text": "Actually, the health check reveals a real issue."}
                yield {"type": "tool_call", "id": "c2", "name": "system_health_read", "arguments": "{}"}
            else:
                yield {"type": "content", "text": "Grounded final answer."}

    model = ClaimThenCallModel()
    orchestrator = AssistantOrchestrator(model, app.state.registry, app.state.gateway)
    events = asyncio.run(drain(orchestrator, base_messages()))
    streamed = content_text(events)
    assert "no issues at all" not in streamed
    assert "reveals a real issue" not in streamed
    assert "Grounded final answer" in streamed


# --- H. Exact historical reproduction: repo analysis on an explicit path ---

def test_explicit_repository_path_analysis_does_not_derail_or_hit_a_naked_round_limit(app, client):
    class RepoAnalysisModel:
        step = 0
        seen_tools: list[str] = []

        async def stream_events(self, messages, tools=None):
            self.step += 1
            names = {item["function"]["name"] for item in (tools or [])}
            self.seen_tools = sorted(names)
            if self.step == 1:
                assert "engineering_repo_map" in names
                yield {"type": "tool_call", "id": "c1", "name": "engineering_repo_map", "arguments": json.dumps({"path": r"X:\XV12"})}
            elif self.step == 2:
                yield {"type": "tool_call", "id": "c2", "name": "engineering_code_search", "arguments": json.dumps({"query": "def create_app", "path": r"X:\XV12"})}
            elif self.step == 3:
                yield {"type": "tool_call", "id": "c3", "name": "engineering_git_status", "arguments": json.dumps({"path": r"X:\XV12"})}
            else:
                yield {"type": "content", "text": "I mapped the repository, searched for the app factory, and checked Git status. No blocking gaps found; a few areas could use more tests."}

    login(client, "admin")
    conversation = create_conversation(client)
    model = RepoAnalysisModel()
    app.state.model = model
    response = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "Analyze your app and let me know what problems or gaps there may be in your code.\n\nX:\\XV12"},
    )
    assert response.status_code == 200
    assert "safe tool-call limit" not in response.text
    assert "I mapped the repository" in response.text
    assert "media_image_generate" not in model.seen_tools
    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    assert stored["messages"][-1]["status"] == "complete"


# --- Contract: legacy naked-round-limit string must not exist any more ---

def test_looping_model_no_longer_produces_the_naked_safe_tool_call_limit_string(app, client):
    class LoopingModel:
        async def stream_events(self, messages, tools=None):
            if tools:
                yield {"type": "tool_call", "id": str(uuid.uuid4()), "name": "system_health_read", "arguments": "{}"}
            else:
                yield {"type": "content", "text": "Here is a grounded summary of what was checked before I stopped."}

    login(client, "admin")
    conversation = create_conversation(client)
    app.state.model = LoopingModel()
    response = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": "Loop tools"})
    assert "I reached the safe tool-call limit before I could finish that request." not in response.text
    assert "grounded summary" in response.text
