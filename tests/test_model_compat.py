from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.model_compat import ToolCallCompatibilityModel, parse_text_tool_calls


pytestmark = [pytest.mark.artifacts, pytest.mark.capability_registry]


def test_textual_qwen_tool_fallback_is_allowlisted_and_decoded() -> None:
    raw = """<function=adas_si_search>
<parameter=query>
2018 Audi A5 lane change assist calibration procedure
</parameter>
</function>
</tool_call>"""
    calls = parse_text_tool_calls(raw, {"adas_si_search"})
    assert len(calls) == 1
    assert calls[0]["name"] == "adas_si_search"
    assert "2018 Audi A5" in calls[0]["arguments"]
    assert parse_text_tool_calls(raw, {"web_current_search"}) == []


def test_compatibility_stream_hides_raw_tool_syntax_and_preserves_model_choice() -> None:
    class TextFallbackModel:
        async def stream_events(self, messages, tools=None) -> AsyncIterator[dict]:
            for chunk in ("I'll retrieve it.\n<fun", "ction=artifact_recent_read>\n<parameter=action>display", "</parameter>\n</function>\n</tool_call>"):
                yield {"type": "content", "text": chunk}

    async def collect() -> list[dict]:
        model = ToolCallCompatibilityModel(TextFallbackModel())
        return [event async for event in model.stream_events([], tools=[{"function": {"name": "artifact_recent_read"}}])]

    events = asyncio.run(collect())
    visible = "".join(str(item.get("text") or "") for item in events if item["type"] == "content")
    calls = [item for item in events if item["type"] == "tool_call"]
    assert visible == "I'll retrieve it.\n"
    assert "<function" not in visible and calls[0]["name"] == "artifact_recent_read"
    assert calls[0]["arguments"] == '{"action": "display"}'
