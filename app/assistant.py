from __future__ import annotations

import inspect
import json
import re
import time
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

# Bounded controls for the ordinary conversation orchestrator. Deliberately smaller than
# Builder's (20 rounds / 1200s): ordinary chat tool calls are fast reads/searches, not
# sandboxed builds or browser validation, and normal X should stay responsive. Named
# constants rather than magic numbers so future benchmarking has a single place to tune.
ASSISTANT_MODEL_ROUND_LIMIT = 10
ASSISTANT_HARD_OPERATION_LIMIT = 22
ASSISTANT_WALL_TIME_LIMIT_SECONDS = 90
# An identical (capability, arguments) call may actually execute this many times *beyond*
# its first execution before further identical repeats are served from the cached result
# instead of hitting the handler again.
ASSISTANT_DUPLICATE_EXECUTION_LIMIT = 1

# Response token budgets are task-appropriate rather than one global number: ordinary
# conversation keeps the existing fast/short budget, deep technical analysis (repository
# inspection, architecture review) gets a moderately larger bounded budget. Builder keeps
# its own separate, larger BUILDER_MODEL_MAX_TOKENS (app/builder_execution.py).
ASSISTANT_RESPONSE_TOKENS_ORDINARY = 768
ASSISTANT_RESPONSE_TOKENS_TECHNICAL = 1536
_TECHNICAL_ANALYSIS_SIGNAL = re.compile(
    r"\b(?:architecture|codebase|repository|repo\b|source\s+code|analyze\s+(?:the|your|this)\s+(?:app|code|repo)|"
    r"security\s+review|technical\s+(?:analysis|review)|engineering)\b", re.I,
)


def _response_token_budget(user_message: str) -> int:
    return ASSISTANT_RESPONSE_TOKENS_TECHNICAL if _TECHNICAL_ANALYSIS_SIGNAL.search(user_message) else ASSISTANT_RESPONSE_TOKENS_ORDINARY


EVIDENCE_SOURCES_LIMIT = 12
EVIDENCE_OBSERVATIONS_LIMIT = 12
EVIDENCE_OBSERVATION_CHAR_LIMIT = 300
EVIDENCE_RECEIPTS_LIMIT = 6


def build_evidence_snapshot(target: str, cards: list[dict[str, Any]], stop_reason: str, next_action: str) -> dict[str, Any]:
    """Bounded structured evidence for a turn that stopped before natural completion, so a
    follow-up 'Continue.' can pick up without rediscovering everything. Deliberately not a
    raw replay of tool payloads and never hidden reasoning -- only capability ids inspected,
    short observations, and receipts."""
    sources: list[str] = []
    observations: list[str] = []
    receipts: list[dict[str, str]] = []
    for card in cards:
        capability_id = str(card.get("capability_id") or "")
        if capability_id and capability_id not in sources:
            sources.append(capability_id)
        result = card.get("result") or {}
        status = str(result.get("status") or "")
        summary = str(result.get("message") or result.get("summary") or "")[:EVIDENCE_OBSERVATION_CHAR_LIMIT]
        if summary:
            observations.append(f"{capability_id} ({status}): {summary}")
        receipts.append({"capability_id": capability_id, "status": status})
    return {
        "target": target[:400],
        "sources_inspected": sources[:EVIDENCE_SOURCES_LIMIT],
        "observations": observations[:EVIDENCE_OBSERVATIONS_LIMIT],
        "unresolved": [f"Stopped before finishing: {stop_reason}"],
        "last_receipts": receipts[-EVIDENCE_RECEIPTS_LIMIT:],
        "stop_reason": stop_reason,
        "next_action": next_action[:400] or "Continue the investigation from the sources and observations above.",
    }


def _call_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    return tool_name + "|" + json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)


def _stream_events_accepts_max_tokens(model: Any) -> bool:
    """Compatibility probe: only pass max_tokens through to models that declare support
    for it, so existing and third-party model adapters with a fixed stream_events(messages,
    tools=None) signature keep working unchanged."""
    try:
        return "max_tokens" in inspect.signature(model.stream_events).parameters
    except (TypeError, ValueError):
        return False


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
    """A bounded, model-directed function loop around the protected conversation stream.

    Distinct control axes (model rounds, operations, wall time, duplicate suppression) so a
    single model response emitting many tool calls, or a slow/looping model, cannot exceed
    its budget through one axis while starving another. When any bound is reached before the
    model naturally finishes, the loop performs exactly one additional generation with tools
    disabled so the gathered evidence is never silently discarded -- see _final_synthesis.
    """

    def __init__(self, model: Any, registry: CapabilityRegistry, gateway: CapabilityGateway) -> None:
        self.model = model
        self.registry = registry
        self.gateway = gateway

    async def _execute_call(
        self,
        call: dict[str, Any],
        user: dict[str, Any],
        cards: list[dict[str, Any]],
        call_counts: dict[str, int],
        call_cache: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Execute (or suppress/reuse) one tool call. Returns (assistant_call, tool_message, card)."""
        call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        tool_name = str(call.get("name") or "")
        try:
            arguments = json.loads(str(call.get("arguments") or "{}"))
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
        except (json.JSONDecodeError, ValueError) as error:
            arguments = {}
            result: dict[str, Any] = {"status": "invalid_arguments", "error": str(error)}
            capability_id = tool_name
        else:
            signature = _call_signature(tool_name, arguments)
            count = call_counts.get(signature, 0)
            if count > ASSISTANT_DUPLICATE_EXECUTION_LIMIT and signature in call_cache:
                try:
                    capability_id = self.registry.capability_id_for_tool(tool_name)
                except (CapabilityNotFound, KeyError):
                    capability_id = tool_name
                result = {
                    **call_cache[signature],
                    "status": "duplicate_suppressed",
                    "message": "This exact capability call already executed with these arguments in this turn; reusing its prior result instead of repeating it.",
                }
            else:
                try:
                    capability_id = self.registry.capability_id_for_tool(tool_name)
                    result, decision = await self.gateway.execute(capability_id, user, arguments)
                    result = {"authorization": decision.reason, **result} if isinstance(result, dict) else {"result": result}
                except CapabilityDenied:
                    capability_id = tool_name
                    result = {"status": "denied", "message": "This capability is not authorized for the authenticated user."}
                except (CapabilityNotFound, KeyError) as error:
                    capability_id = tool_name
                    result = {"status": "unavailable", "message": str(error)}
                except Exception as error:
                    capability_id = tool_name
                    result = {"status": "failed", "error": type(error).__name__}
                call_counts[signature] = count + 1
                call_cache[signature] = result
        card = {"capability_id": capability_id, "arguments": arguments, "result": result}
        cards.append(card)
        assistant_call = {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": json.dumps(arguments, ensure_ascii=False)}}
        tool_message = {"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": json.dumps(result, ensure_ascii=False, default=str)[:24000]}
        return assistant_call, tool_message, card

    @staticmethod
    def _budget_exhausted_message(call: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
        tool_name = str(call.get("name") or "")
        result = {"status": "budget_exhausted", "message": "The operation budget for this turn was reached before this call could run."}
        assistant_call = {"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": str(call.get("arguments") or "{}")}}
        tool_message = {"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": json.dumps(result, ensure_ascii=False)}
        return assistant_call, tool_message

    @staticmethod
    async def _final_synthesis(model: Any, working: list[dict[str, Any]], stop_reason: str) -> str:
        """One additional generation with tools disabled, using every message/evidence
        gathered so far. Never skipped when a bound was hit -- the last tool results must
        never be thrown away just because the budget ended."""
        nudge = {
            "role": "user",
            "content": (
                "You reached a safe execution bound (" + stop_reason + ") before finishing this investigation. "
                "Tools are now disabled for this final reply. Using only the evidence already gathered above, give "
                "the best truthful response you can. Explicitly summarize: what you inspected, what you found, "
                "what remains uncertain, and why you stopped. Do not claim to have verified anything beyond what "
                "the tool results above actually show."
            ),
        }
        prompt = [*working, nudge]
        text_parts: list[str] = []
        if hasattr(model, "stream_events"):
            async for event in model.stream_events(prompt, tools=[]):
                if event.get("type") == "content":
                    text_parts.append(str(event.get("text") or ""))
        text = "".join(text_parts).strip()
        return text

    async def stream(
        self,
        messages: list[dict[str, Any]],
        user: dict[str, Any],
        max_rounds: int = ASSISTANT_MODEL_ROUND_LIMIT,
        max_operations: int = ASSISTANT_HARD_OPERATION_LIMIT,
        wall_time_seconds: float = ASSISTANT_WALL_TIME_LIMIT_SECONDS,
    ) -> AsyncIterator[dict[str, Any]]:
        tools = self.registry.model_tools(user)
        working = [dict(item) for item in messages]
        last_user_message = next(
            (str(item.get("content") or "") for item in reversed(working) if item.get("role") == "user"),
            "",
        )
        response_tokens = _response_token_budget(last_user_message)
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
        started = time.monotonic()
        operation_count = 0
        model_rounds = 0
        call_counts: dict[str, int] = {}
        call_cache: dict[str, dict[str, Any]] = {}
        stop_reason = "natural_completion"

        def telemetry() -> dict[str, Any]:
            return {
                "model_rounds": model_rounds,
                "operation_count": operation_count,
                "capability_count": len(cards),
            }

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
            if time.monotonic() - started > wall_time_seconds:
                stop_reason = "wall_time_limit"
                break
            if operation_count >= max_operations:
                stop_reason = "operation_limit"
                break

            model_rounds = round_index + 1
            calls: list[dict[str, Any]] = []
            guard_live_synthesis = forced_web_result is not None and round_index == 0
            buffered_content: list[str] = []
            round_content: list[str] = []
            if not hasattr(self.model, "stream_events"):
                async for text in self.model.stream(working):
                    yield {"type": "content", "text": text}
                return
            if _stream_events_accepts_max_tokens(self.model):
                round_events = self.model.stream_events(working, tools=tools, max_tokens=response_tokens)
            else:
                round_events = self.model.stream_events(working, tools=tools)
            async for event in round_events:
                if event.get("type") == "content":
                    text = str(event.get("text") or "")
                    if guard_live_synthesis:
                        buffered_content.append(text)
                    else:
                        round_content.append(text)
                elif event.get("type") == "tool_call":
                    calls.append(event)

            if guard_live_synthesis:
                synthesized = "".join(buffered_content).strip()
                if _FALSE_LIVE_ACCESS_DENIAL.search(synthesized) or (not synthesized and not calls):
                    synthesized = _live_web_fallback(forced_web_result)
                if synthesized:
                    yield {"type": "content", "text": synthesized}

            if not calls:
                text = "".join(round_content)
                if text:
                    yield {"type": "content", "text": text}
                yield {
                    "type": "complete", "cards": cards, "status": "complete", "stop_reason": "natural_completion",
                    "telemetry": {**telemetry(), "final_synthesis_performed": False},
                }
                return

            # A claim made in the same round as its own tool call is not yet grounded in that
            # call's evidence -- discard it rather than streaming an unsupported claim to the
            # user. Only content from a round with no accompanying tool calls is trustworthy.

            assistant_calls: list[dict[str, Any]] = []
            tool_messages: list[dict[str, Any]] = []
            for call in calls:
                if operation_count >= max_operations:
                    assistant_call, tool_message = self._budget_exhausted_message(call)
                else:
                    operation_count += 1
                    call_id = str(call.get("id") or f"call_{uuid.uuid4().hex}")
                    tool_name = str(call.get("name") or "")
                    yield {"type": "capability_start", "capability_id": tool_name, "arguments": {}}
                    assistant_call, tool_message, card = await self._execute_call(call, user, cards, call_counts, call_cache)
                    yield {"type": "capability_result", **card}
                assistant_calls.append(assistant_call)
                tool_messages.append(tool_message)
            working.append({"role": "assistant", "content": None, "tool_calls": assistant_calls})
            working.extend(tool_messages)

            if operation_count >= max_operations:
                stop_reason = "operation_limit"
                break
        else:
            stop_reason = "model_round_limit"

        final_text = await self._final_synthesis(self.model, working, stop_reason)
        if final_text:
            yield {"type": "content", "text": final_text}
        yield {
            "type": "complete", "cards": cards,
            "status": "partial_success" if final_text else "budget_exhausted",
            "stop_reason": stop_reason,
            "telemetry": {**telemetry(), "final_synthesis_performed": True},
        }
