from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.builder_execution import BUILDER_RAW_TAIL_MESSAGES, CONTEXT_CHARACTER_LIMIT, BuilderExecutionService, default_task_state


pytestmark = pytest.mark.creator


def _tool_round(call_count: int, payload_chars: int) -> list[dict]:
    """One atomic exchange: one assistant message declaring N tool calls, followed by their
    N matching tool result messages -- the unit that must never be split across the raw-tail
    boundary."""
    calls = []
    tool_messages = []
    for _ in range(call_count):
        call_id = f"call_{uuid.uuid4().hex}"
        calls.append({"id": call_id, "type": "function", "function": {"name": "builder_workspace_inspect", "arguments": "{}"}})
        tool_messages.append({
            "role": "tool", "tool_call_id": call_id, "name": "builder_workspace_inspect",
            "content": json.dumps({"status": "success", "detail": "x" * payload_chars}),
        })
    return [{"role": "assistant", "content": None, "tool_calls": calls}, *tool_messages]


def _assert_no_orphaned_tool_messages(messages: list[dict]) -> None:
    known_call_ids: set[str] = set()
    for message in messages:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            known_call_ids.update(str(call.get("id")) for call in message["tool_calls"])
        elif role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "")
            assert tool_call_id in known_call_ids, (
                f"Orphaned tool message references tool_call_id={tool_call_id!r} with no preceding "
                "assistant message declaring that call -- this would be rejected by an OpenAI-style "
                "chat completions API."
            )


def test_compaction_never_produces_an_orphaned_tool_message():
    """Construct a message history where the fixed-size raw-tail boundary
    (BUILDER_RAW_TAIL_MESSAGES) is deliberately misaligned with tool-call-group boundaries by
    using varying group sizes, then assert the reconstructed context never separates a tool
    result from the assistant message that declared its call."""
    system = [{"role": "system", "content": "system contract"}]
    body: list[dict] = []
    group_sizes = [3, 5, 2, 7, 4, 6, 3, 5, 2, 7, 4, 6, 3, 5]
    for size in group_sizes:
        body.extend(_tool_round(size, payload_chars=2200))
    messages = system + body
    assert len(json.dumps(messages, ensure_ascii=False, default=str)) > CONTEXT_CHARACTER_LIMIT

    class NoCompleteModel:
        async def complete(self, _messages, max_tokens=400):
            return "summary of earlier engineering history"

    service = object.__new__(BuilderExecutionService)
    reconstructed, _size = asyncio.run(
        BuilderExecutionService._compact_engineering_context(
            service, messages, NoCompleteModel(), {"original_request": "test"}, default_task_state("test"),
        )
    )
    _assert_no_orphaned_tool_messages(reconstructed)


def test_raw_tail_boundary_is_group_aware_not_a_blind_slice():
    """If BUILDER_RAW_TAIL_MESSAGES messages were kept as a pure positional slice, a
    misaligned cut would start mid tool-call-group. Prove the reconstructed tail either starts
    at a group boundary (an assistant or user message) or, if it starts with a tool message,
    that message's declaring assistant message was pulled in too."""
    system = [{"role": "system", "content": "system contract"}]
    body: list[dict] = []
    for size in [4, 4, 4, 4, 4, 4, 4]:  # 7 groups x 5 messages = 35 body messages; -12 cuts mid-group under a naive slice
        body.extend(_tool_round(size, payload_chars=4000))
    messages = system + body
    assert len(json.dumps(messages, ensure_ascii=False, default=str)) > CONTEXT_CHARACTER_LIMIT
    naive_tail = body[-BUILDER_RAW_TAIL_MESSAGES:]
    assert naive_tail[0]["role"] == "tool", "test fixture must actually reproduce a naive mid-group cut"

    class NoCompleteModel:
        async def complete(self, _messages, max_tokens=400):
            return ""

    service = object.__new__(BuilderExecutionService)
    reconstructed, _size = asyncio.run(
        BuilderExecutionService._compact_engineering_context(
            service, messages, NoCompleteModel(), {"original_request": "test"}, default_task_state("test"),
        )
    )
    _assert_no_orphaned_tool_messages(reconstructed)
