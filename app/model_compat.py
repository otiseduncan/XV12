from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any


FUNCTION_OPEN = re.compile(r"<function=([A-Za-z0-9_]+)>", re.IGNORECASE)
FUNCTION_BLOCK = re.compile(
    r"<function=([A-Za-z0-9_]+)>\s*(.*?)(?:</function>|</tool_call>|$)",
    re.IGNORECASE | re.DOTALL,
)
PARAMETER_BLOCK = re.compile(
    r"<parameter=([A-Za-z0-9_]+)>\s*(.*?)\s*</parameter>",
    re.IGNORECASE | re.DOTALL,
)


def _value(text: str) -> Any:
    value = text.strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_text_tool_calls(text: str, allowed_names: set[str]) -> list[dict[str, Any]]:
    """Decode the Qwen textual fallback without inventing or rerouting a tool choice."""

    calls: list[dict[str, Any]] = []
    for match in FUNCTION_BLOCK.finditer(text):
        name = match.group(1)
        if name not in allowed_names:
            continue
        body = match.group(2)
        arguments = {item.group(1): _value(item.group(2)) for item in PARAMETER_BLOCK.finditer(body)}
        if not arguments:
            raw = re.sub(r"</?tool_call>", "", body, flags=re.IGNORECASE).strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    arguments = parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    arguments = {}
        calls.append(
            {
                "type": "tool_call",
                "id": f"compat_{uuid.uuid4().hex}",
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }
        )
    return calls


class ToolCallCompatibilityModel:
    """Preserve native calls and safely decode a known local-model fallback syntax."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def __getattr__(self, name: str) -> Any:
        return getattr(self.model, name)

    async def health(self) -> dict[str, Any]:
        return await self.model.health()

    async def complete(self, messages: list[dict[str, Any]], max_tokens: int = 320) -> str:
        return await self.model.complete(messages, max_tokens=max_tokens)

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        async for event in self.stream_events(messages):
            if event.get("type") == "content":
                yield str(event.get("text") or "")

    async def stream_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        allowed_names = {
            str(item.get("function", {}).get("name") or "")
            for item in (tools or [])
            if item.get("function", {}).get("name")
        }
        pending = ""
        capturing = False
        async for event in self.model.stream_events(messages, tools=tools):
            if event.get("type") != "content" or not allowed_names:
                if pending and not capturing:
                    yield {"type": "content", "text": pending}
                    pending = ""
                yield event
                continue
            pending += str(event.get("text") or "")
            if capturing:
                continue
            match = FUNCTION_OPEN.search(pending)
            if match:
                prefix = pending[:match.start()]
                if prefix:
                    yield {"type": "content", "text": prefix}
                pending = pending[match.start():]
                capturing = True
            elif len(pending) > 32:
                yield {"type": "content", "text": pending[:-32]}
                pending = pending[-32:]
        if capturing:
            calls = parse_text_tool_calls(pending, allowed_names)
            if calls:
                for call in calls:
                    yield call
            else:
                yield {"type": "content", "text": "I couldn't complete that capability call safely."}
        elif pending:
            yield {"type": "content", "text": pending}
