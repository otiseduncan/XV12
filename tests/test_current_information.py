from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.current_information import assess_current_information
from .conftest import create_conversation, login


@pytest.mark.web
@pytest.mark.parametrize(
    ("message", "required", "mode"),
    [
        ("what are the most recent updates with Iran", True, "news"),
        ("Anything new about Iran?", True, "news"),
        ("What is the current president of France?", True, "general"),
        ("Explain electrical current in a DC motor.", False, "current"),
        ("What is the current status of Calibration IQ?", False, "current"),
    ],
)
def test_current_information_boundary_is_high_confidence(message, required, mode):
    requirement = assess_current_information(message)
    assert requirement.required is required
    assert requirement.mode == mode


@pytest.mark.web
@pytest.mark.chat_core
def test_recent_iran_updates_force_live_web_and_block_false_capability_denial(client, app):
    class RefusingModel:
        def __init__(self) -> None:
            self.requests = []

        async def stream_events(self, messages, tools=None) -> AsyncIterator[dict]:
            self.requests.append({"messages": messages, "tools": tools})
            yield {
                "type": "content",
                "text": (
                    "I don't have access to live news or current events updates, including recent "
                    "developments with Iran. I recommend checking reliable news sources."
                ),
            }

        async def stream(self, messages):
            yield "unused"

        async def complete(self, messages, max_tokens=320):
            return "summary"

        async def health(self):
            return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

    web_calls = []

    def live_web(arguments):
        web_calls.append(dict(arguments))
        return {
            "status": "verified_results",
            "query": arguments["query"],
            "mode": arguments["mode"],
            "executed_at": "2026-08-11T08:30:00+00:00",
            "provider": "Bing News RSS",
            "results": [
                {
                    "title": "Iran update from live search",
                    "url": "https://example.test/iran",
                    "snippet": "Fresh evidence returned by the live provider.",
                    "published_at": "Tue, 11 Aug 2026 08:00:00 GMT",
                    "source": "Bing News RSS",
                    "reference": "web:1",
                }
            ],
            "evidence": {"executed": True, "result_count": 1},
        }

    login(client)
    app.state.model = RefusingModel()
    app.state.gateway.handlers["web.current.search"] = live_web
    conversation = create_conversation(client)

    body = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "what are the most recent updates with Iran"},
    ).text

    assert len(web_calls) == 1
    assert web_calls[0] == {
        "query": "what are the most recent updates with Iran",
        "mode": "news",
        "limit": 5,
    }
    assert "event: capability" in body
    assert "web.current.search" in body
    assert "Iran update from live search" in body
    assert "web:1" in body
    assert "don't have access to live news" not in body.lower()

    stored = client.get(f"/api/conversations/{conversation['id']}").json()
    assistant = stored["messages"][-1]
    assert assistant["status"] == "complete"
    assert assistant["metadata"]["capability_cards"][0]["capability_id"] == "web.current.search"
    assert assistant["metadata"]["capability_cards"][0]["result"]["domain_status"] == "verified_results"

    model_request = app.state.model.requests[0]
    assert any(item["role"] == "tool" and item["name"] == "web_current_search" for item in model_request["messages"])
    assert all(
        item.get("function", {}).get("name") != "web_current_search"
        for item in (model_request["tools"] or [])
    )
