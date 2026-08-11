from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from .current_information import assess_current_information
from .registry import CapabilityDenied, CapabilityGateway, CapabilityNotFound, CapabilityRegistry, TRUTH_CONTRACT


_FALSE_LIVE_ACCESS_DENIAL = re.compile(
    r"\b(?:i\s+(?:do\s+not|don't|cannot|can't)\s+(?:have\s+)?access\s+to\s+(?:the\s+)?(?:live\s+)?(?:web|internet|news|current\s+events?)|"
    r"i\s+(?:cannot|can't)\s+(?:browse|search|access)\s+(?:the\s+)?(?:web|internet)|"
    r"no\s+access\s+to\s+(?:live\s+)?(?:news|current\s+events?|the\s+web|the\s+internet))\b",
    re.I,
)


def _live_web_fallback(result: dict[str, Any]) -> str:
    """Produce a truthful bounded answer when the model contradicts an executed web receipt."""

    results = result.get("results") if isinstance(result.get("results"), list) else []
    if results:
        lines = ["I ran a live web search. Here are the most recent results it returned:"]
        for index, item in enumerate(results[:5], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or f"Result {index}").strip()
            snippet = str(item.get("snippet") or "").strip()
            reference = str(item.get("reference") or f"web:{index}").strip()
            detail = f" — {snippet}" if snippet else ""
            lines.append(f"- {title}{detail} [{reference}]")
        provider = str(result.get("provider") or "").strip()
        executed_at = str(result.get("executed_at") or "").strip()
        if provider or executed_at:
            suffix = " · ".join(value for value in (provider, executed_at) if value)
            lines.append(f"\nLive-search receipt: {suffix}.")
        return "\n".join(lines)
    if result.get("status") == "no_result":
        return "I ran the live web search, but it returned no current results for that query."
    return "I tried the live web search, but the provider could not return current evidence right now."


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
        last_user_message = next(
            (str(item.get("content") or "") for item in reversed(working) if item.get("role") == "user"),
            "",
        )
        freshness = assess_current_information(last_user_message)
        web_tool_name = self.registry.tool_name("web.current.search")
        web_available = any(
            str(item.get("function", {}).get("name") or "") == web_tool_name
            for item in tools
        )
        if tools and working and working[0].get("role") == "system":
            working[0]["content"] += (
                "\n\nThe function definitions supplied with this request are the authoritative capabilities currently available to you. "
                "Choose and call them yourself when they are relevant; do not claim their result before receiving it. "
                "When you decide to call a function, emit the native function call only in that turn: do not put introductory prose, XML-like tags, or a textual function-call imitation before or around it. "
                "Any requested state change must be performed with the matching function; never narrate that a project, setting, or service changed unless a successful function result in this turn proves it. "
                "When the user asks what capabilities exist in a particular family (for example databases), include only functions whose registry family label matches that family; do not relabel system health or another family as a database. "
                "A database status of no_result is a bounded local miss, not proof that the fact does not exist. "
                "When the user needs current or external information after such a miss, you may call the live web search function in a later round. "
                f"Evidence rule: {TRUTH_CONTRACT} "
                "Use service-start functions only after an explicit request from the authenticated administrator."
            )

        cards: list[dict[str, Any]] = []
        forced_web_result: dict[str, Any] | None = None

        # Freshness is a truth boundary, not a model preference. A high-confidence current-
        # information request must obtain a live receipt before the local model synthesizes it.
        if freshness.required and web_available:
            call_id = f"fresh_{uuid.uuid4().hex}"
            arguments = {"query": last_user_message, "mode": freshness.mode, "limit": 5}
            capability_id = "web.current.search"
            yield {"type": "capability_start", "capability_id": capability_id, "arguments": arguments}
            try:
                result, decision = await self.gateway.execute(capability_id, user, arguments)
                result = {"authorization": decision.reason, **result} if isinstance(result, dict) else {"result": result}
            except CapabilityDenied:
                result = {"status": "denied", "message": "This capability is not authorized for the authenticated user."}
            except (CapabilityNotFound, KeyError) as error:
                result = {"status": "unavailable", "message": str(error)}
            except Exception as error:
                result = {"status": "failed", "error": type(error).__name__}
            forced_web_result = result
            card = {"capability_id": capability_id, "arguments": arguments, "result": result}
            cards.append(card)
            yield {"type": "capability_result", **card}
            working.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": web_tool_name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            )
            working.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": web_tool_name,
                    "content": json.dumps(result, ensure_ascii=False, default=str)[:24000],
                }
            )
            # The live search already executed exactly once for this turn. Keep all other
            # authorized capabilities available, but do not let the model repeat this search.
            tools = [
                item
                for item in tools
                if str(item.get("function", {}).get("name") or "") != web_tool_name
            ]
            if working and working[0].get("role") == "system":
                working[0]["content"] += (
                    "\n\nA live web search was already executed for this turn because the user's request requires fresh information. "
                    "Synthesize the returned evidence directly. Do not claim that you lack live-news, web, internet, or current-events access. "
                    "Use the returned web:n references when discussing specific search results. If the receipt has no results or is unavailable, say that accurately."
                )

        for round_index in range(max_rounds):
            calls: list[dict[str, Any]] = []
            guard_live_synthesis = forced_web_result is not None and round_index == 0
            buffered_content: list[str] = []
            if hasattr(self.model, "stream_events"):
                async for event in self.model.stream_events(working, tools=tools):
                    if event.get("type") == "content":
                        text = str(event.get("text") or "")
                        if guard_live_synthesis:
                            buffered_content.append(text)
                        else:
                            yield {"type": "content", "text": text}
                    elif event.get("type") == "tool_call":
                        calls.append(event)
            else:
                async for text in self.model.stream(working):
                    yield {"type": "content", "text": text}
                return

            if guard_live_synthesis:
                synthesized = "".join(buffered_content).strip()
                if _FALSE_LIVE_ACCESS_DENIAL.search(synthesized) or (not synthesized and not calls):
                    synthesized = _live_web_fallback(forced_web_result)
                if synthesized:
                    yield {"type": "content", "text": synthesized}

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
