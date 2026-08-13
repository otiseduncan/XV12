from __future__ import annotations

import inspect
import json
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import PureWindowsPath
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
TEXT_TOOL_PREFIX_WINDOW = 256
WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s\"']*", re.IGNORECASE)
ROOT_TARGET_TOOLS = {
    "engineering_repo_map",
    "engineering_code_search",
    "engineering_git_status",
    "engineering_git_diff",
    "engineering_tests_inspect",
    "files_local_read",
}


def _accepts_max_tokens(model: Any) -> bool:
    try:
        return "max_tokens" in inspect.signature(model.stream_events).parameters
    except (TypeError, ValueError):
        return False


def _explicit_windows_path(message: str) -> str | None:
    """Return the first explicit Windows filesystem target exactly as the user supplied it.

    This is intentionally syntax-only; authorization still belongs to the capability itself.
    The compatibility layer uses the value only to stop a model/tool-call serialization error
    from broadening ``X:\\XV12`` into ``X:\\`` before the capability sees it.
    """
    match = WINDOWS_PATH.search(message or "")
    if not match:
        return None
    target = match.group(0).rstrip(".,;:)]}")
    return target or None


def _is_broader_windows_path(candidate: str, target: str) -> bool:
    """True when candidate is a strict Windows-path ancestor of target on the same drive."""
    try:
        candidate_path = PureWindowsPath(candidate)
        target_path = PureWindowsPath(target)
    except (TypeError, ValueError):
        return False
    if candidate_path.drive.casefold() != target_path.drive.casefold():
        return False
    candidate_parts = tuple(part.casefold() for part in candidate_path.parts)
    target_parts = tuple(part.casefold() for part in target_path.parts)
    return len(candidate_parts) < len(target_parts) and target_parts[: len(candidate_parts)] == candidate_parts


def preserve_explicit_path_target(event: dict[str, Any], user_message: str) -> dict[str, Any]:
    """Prevent root-scoped file/engineering calls from silently broadening a user path.

    Qwen may occasionally emit ``X:\\`` even when the user supplied ``X:\\XV12``. The broad
    drive root is correctly rejected by RepoInspectionService, but the rejection is misleading
    because the user never requested the drive root. When a root-level inspection call omits
    its path or supplies a strict ancestor of the explicit target, pin it back to the exact
    user target. More-specific descendant paths are preserved unchanged for follow-up reads.

    This does not grant access: the downstream capability still performs its normal root and
    sensitive-path authorization.
    """
    if event.get("type") != "tool_call" or str(event.get("name") or "") not in ROOT_TARGET_TOOLS:
        return event
    target = _explicit_windows_path(user_message)
    if not target:
        return event
    raw_arguments = event.get("arguments")
    try:
        arguments = raw_arguments if isinstance(raw_arguments, dict) else json.loads(str(raw_arguments or "{}"))
    except json.JSONDecodeError:
        return event
    if not isinstance(arguments, dict):
        return event
    current = str(arguments.get("path") or "").strip()
    if current and not _is_broader_windows_path(current, target):
        return event
    repaired = dict(arguments)
    repaired["path"] = target
    return {**event, "arguments": json.dumps(repaired, ensure_ascii=False)}


def artifact_followup_arguments(message: str, allowed_names: set[str]) -> dict[str, Any] | None:
    folded = message.casefold().strip()
    if "artifact_recent_read" not in allowed_names:
        return None
    retrieval_tools = {"adas_si_search", "adas_knowledge_search", "web_current_search", "files_local_read"}
    if allowed_names & retrieval_tools:
        return None
    action = next((name for name in ("display", "view", "open", "print", "download", "copy", "show") if name in folded), None)
    referential = bool(re.search(r"\b(?:document|manual|pdf|file|section|page)\b|\b(?:open|print|download|copy|display)\s+it\b", folded))
    if not action or not referential:
        return None
    arguments: dict[str, Any] = {"action": {"open": "view", "show": "display"}.get(action, action)}
    page = re.search(r"\bpage\s+(\d+)\b", folded)
    if page:
        arguments.update({"scope": "page", "page": int(page.group(1))})
    elif re.search(r"\b(?:whole|entire|complete|full)\b.*\b(?:document|manual|pdf|file)\b", folded):
        arguments["scope"] = "full"
    elif re.search(r"\b(?:whole|entire|complete|full)\b.*\bsection\b", folded):
        arguments["scope"] = "section"
    else:
        arguments["scope"] = "current"
    return arguments


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
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        allowed_names = {
            str(item.get("function", {}).get("name") or "")
            for item in (tools or [])
            if item.get("function", {}).get("name")
        }
        last_user_message = next((str(item.get("content") or "") for item in reversed(messages) if item.get("role") == "user"), "")
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        required_artifact_arguments = None if has_tool_result else artifact_followup_arguments(last_user_message, allowed_names)
        pending = ""
        capturing = False
        emitted_tool_call = False
        inner = self.model.stream_events(messages, tools=tools, max_tokens=max_tokens) if _accepts_max_tokens(self.model) else self.model.stream_events(messages, tools=tools)
        async for event in inner:
            if event.get("type") != "content" or not allowed_names:
                if event.get("type") == "tool_call":
                    event = preserve_explicit_path_target(event, last_user_message)
                    emitted_tool_call = True
                if pending and not capturing and required_artifact_arguments is None:
                    yield {"type": "content", "text": pending}
                    pending = ""
                yield event
                continue
            pending += str(event.get("text") or "")
            if required_artifact_arguments is not None:
                continue
            if capturing:
                continue
            match = FUNCTION_OPEN.search(pending)
            if match:
                prefix = pending[:match.start()]
                if prefix:
                    yield {"type": "content", "text": prefix}
                pending = pending[match.start():]
                capturing = True
            elif len(pending) > TEXT_TOOL_PREFIX_WINDOW:
                yield {"type": "content", "text": pending[:-TEXT_TOOL_PREFIX_WINDOW]}
                pending = pending[-TEXT_TOOL_PREFIX_WINDOW:]
        if capturing:
            calls = parse_text_tool_calls(pending, allowed_names)
            if calls:
                for call in calls:
                    emitted_tool_call = True
                    yield preserve_explicit_path_target(call, last_user_message)
            else:
                yield {"type": "content", "text": "I couldn't complete that capability call safely."}
        elif pending and required_artifact_arguments is None:
            yield {"type": "content", "text": pending}
        if required_artifact_arguments is not None and not emitted_tool_call:
            yield {
                "type": "tool_call", "id": f"artifact_{uuid.uuid4().hex}", "name": "artifact_recent_read",
                "arguments": json.dumps(required_artifact_arguments, ensure_ascii=False),
            }
