from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .config import Settings


class ModelRuntimeError(RuntimeError):
    pass


class LlamaModel:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(f"{self.settings.model_base_url}/models")
                response.raise_for_status()
                ids = [str(item.get("id")) for item in response.json().get("data", [])]
            return {"reachable": True, "alias_ok": self.settings.model_alias in ids, "models": ids}
        except Exception as error:
            return {"reachable": False, "alias_ok": False, "models": [], "error": type(error).__name__}

    async def stream_events(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        payload = {
            "model": self.settings.model_alias,
            "messages": messages,
            "stream": True,
            "temperature": self.settings.model_temperature,
            "max_tokens": self.settings.model_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        pending_calls: dict[int, dict[str, Any]] = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=300, write=30, pool=15)) as client:
            async with client.stream("POST", f"{self.settings.model_base_url}/chat/completions", json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode("utf-8", errors="replace")[:600]
                    raise ModelRuntimeError(f"Model request failed with HTTP {response.status_code}: {body}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        text = delta.get("content")
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index", 0))
                            current = pending_calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            current["id"] += str(call.get("id") or "")
                            function = call.get("function") or {}
                            current["name"] += str(function.get("name") or "")
                            current["arguments"] += str(function.get("arguments") or "")
                    except (json.JSONDecodeError, IndexError, AttributeError):
                        continue
                    if text:
                        yield {"type": "content", "text": str(text)}
        for index in sorted(pending_calls):
            yield {"type": "tool_call", **pending_calls[index]}

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        async for event in self.stream_events(messages):
            if event["type"] == "content":
                yield str(event["text"])

    async def complete(self, messages: list[dict[str, str]], max_tokens: int = 320) -> str:
        payload = {
            "model": self.settings.model_alias,
            "messages": messages,
            "stream": False,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=180, write=30, pool=15)) as client:
            response = await client.post(f"{self.settings.model_base_url}/chat/completions", json=payload)
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"]).strip()
