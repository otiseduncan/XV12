from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app


class FakeModel:
    def __init__(self, alias: str = "xoduz-qwen3-coder-30b") -> None:
        self.alias = alias
        self.requests: list[list[dict[str, str]]] = []

    async def health(self):
        return {"reachable": True, "alias_ok": True, "models": [self.alias]}

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        self.requests.append(messages)
        user_text = next((item["content"] for item in reversed(messages) if item["role"] == "user"), "")
        for part in ("Good morning. ", "I'm XODUZ, and I'm here with you. ", f"You said: {user_text[:80]}"):
            yield part

    async def complete(self, messages: list[dict[str, str]], max_tokens: int = 320) -> str:
        self.requests.append(messages)
        return "The user is working on Project Atlas and wants continuity across the task."


def make_settings(tmp_path: Path, *, auth_mode: str = "test") -> Settings:
    return Settings(
        root=ROOT,
        app_host="127.0.0.1",
        app_port=8120,
        model_port=8121,
        model_alias="xoduz-qwen3-coder-30b",
        model_context_tokens=32768,
        model_max_tokens=768,
        model_temperature=0.35,
        database_path=tmp_path / "xv12-test.db",
        attachments_path=tmp_path / "attachments",
        auth_mode=auth_mode,
        google_client_id="test-google-client.apps.googleusercontent.com",
        google_client_secret="test-secret",
        google_redirect_uri="http://127.0.0.1:8120/api/auth/google/callback",
        owner_google_sub="test-admin-sub",
        cookie_secure=False,
        session_ttl_seconds=3600,
        comfyui_enabled=False,
    )


@pytest.fixture
def app(tmp_path):
    application = create_app(make_settings(tmp_path))
    application.state.model = FakeModel()
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, persona: str = "admin"):
    response = client.post("/api/auth/test-login", json={"persona": persona})
    assert response.status_code == 200, response.text
    return response.json()


def create_conversation(client: TestClient):
    response = client.post("/api/conversations", json={"title": "New conversation"})
    assert response.status_code == 201, response.text
    return response.json()
