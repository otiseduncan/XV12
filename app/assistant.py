from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .registry import CapabilityDenied, CapabilityGateway, CapabilityNotFound, CapabilityRegistry


class AssistantOrchestrator:
    """A bounded, model-directed function loop around the protected conversation stream."""

    def __init__(self, model: Any, registry: CapabilityRegistry, gateway: CapabilityGateway) -> None:
        self.model = model
        self.registry = registry
        self.gateway = gateway

    async def stream(
        self,
        messages: list[dict[str, Any]],
        user: dict[str, Any],
        max_rounds: int = 4,
    ) -> AsyncIterator[dict[str, Any]]:
        tools = self.registry.model_tools(user)
        working = [dict(item) for item in messages]
        if tools and working and working[0].get("role") == "system":
            working[0]["content"] += (
                "\n\nThe function definitions supplied with this request are the authoritative capabilities currently available to you. "
                "Choose and call them yourself when they are relevant; do not claim their result before receiving it. "
                "When you decide to call a function, emit the native function call only in that turn: do not put introductory prose, XML-like tags, or a textual function-call imitation before or around it. "
                "Any requested state change must be performed with the matching function; never narrate that a project, setting, or service changed unless a successful function result in this turn proves it. "
                "When the user asks what capabilities exist in a particular family (for example databases), include only functions whose registry family label matches that family; do not relabel system health or another family as a database. "
                "A database status of no_result is a bounded local miss, not proof that the fact does not exist. "
                "When the user needs current or external information after such a miss, you may call the live web search function in a later round. "
                "Use service-start functions only after an explicit request from the authenticated administrator."
            )
        cards: list[dict[str, Any]] = []
        for round_index in range(max_rounds):
            calls: list[dict[str, Any]] = []
            if hasattr(self.model, "stream_events"):
                async for event in self.model.stream_events(working, tools=tools):
                    if event.get("type") == "content":
                        yield {"type": "content", "text": str(event.get("text") or "")}
                    elif event.get("type") == "tool_call":
                        calls.append(event)
            else:
                async for text in self.model.stream(working):
                    yield {"type": "content", "text": text}
                return
            if not calls:
                yield {"type": "complete", "cards": cards}
                return
            assistant_calls = []
            tool_messages = []
            for call in calls:
                call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
                tool_name = str(call.get("name") or "")
                try:
                    arguments = json.loads(str(call.get("arguments") or "{}"))
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                except (json.JSONDecodeError, ValueError) as error:
                    arguments = {}
                    result = {"status": "invalid_arguments", "error": str(error)}
                    capability_id = tool_name
                else:
                    try:
                        capability_id = self.registry.capability_id_for_tool(tool_name)
                        yield {"type": "capability_start", "capability_id": capability_id, "arguments": arguments}
                        result, decision = await self.gateway.execute(capability_id, user, arguments)
                        result = {"authorization": decision.reason, **result} if isinstance(result, dict) else {"result": result}
                    except CapabilityDenied:
                        result = {"status": "denied", "message": "This capability is not authorized for the authenticated user."}
                    except (CapabilityNotFound, KeyError) as error:
                        result = {"status": "unavailable", "message": str(error)}
                    except Exception as error:
                        result = {"status": "failed", "error": type(error).__name__}
                card = {"capability_id": capability_id, "arguments": arguments, "result": result}
                cards.append(card)
                yield {"type": "capability_result", **card}
                assistant_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(arguments)},
                    }
                )
                tool_messages.append(
                    {"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": json.dumps(result, ensure_ascii=False, default=str)[:24000]}
                )
            working.append({"role": "assistant", "content": None, "tool_calls": assistant_calls})
            working.extend(tool_messages)
            if round_index == max_rounds - 1:
                yield {"type": "content", "text": "I reached the safe tool-call limit before I could finish that request."}
                yield {"type": "complete", "cards": cards}
