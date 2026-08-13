from __future__ import annotations

import io

import pytest

from .conftest import create_conversation, login


pytestmark = pytest.mark.attachments


class _EchoModel:
    def __init__(self) -> None:
        self.seen_prompt = ""

    async def stream_events(self, messages, tools=None):
        self.seen_prompt = " ".join(str(item.get("content") or "") for item in messages)
        yield {"type": "content", "text": "Reviewed the attached content."}


def test_text_attachment_content_is_ingested_into_the_prompt(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    upload = client.post(
        "/api/attachments",
        files={"file": ("notes.py", io.BytesIO(b"def marker_function():\n    return 'unique-marker-value'\n"), "text/x-python")},
    )
    assert upload.status_code == 201
    attachment_id = upload.json()["id"]

    model = _EchoModel()
    app.state.model = model
    response = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "What does this file do?", "attachment_ids": [attachment_id]},
    )
    assert response.status_code == 200
    assert "unique-marker-value" in model.seen_prompt
    assert "marker_function" in model.seen_prompt


def test_attachment_excerpt_is_bounded_not_a_full_dump(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    large_content = ("line of repeated content for size\n" * 20000).encode("utf-8")
    upload = client.post("/api/attachments", files={"file": ("big.txt", io.BytesIO(large_content), "text/plain")})
    assert upload.status_code == 201
    attachment_id = upload.json()["id"]

    model = _EchoModel()
    app.state.model = model
    response = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "Summarize this file.", "attachment_ids": [attachment_id]},
    )
    assert response.status_code == 200
    assert len(model.seen_prompt) < len(large_content.decode("utf-8"))
    assert "(truncated)" in model.seen_prompt


def test_binary_attachment_falls_back_to_metadata_only(app, client):
    login(client, "admin")
    conversation = create_conversation(client)
    upload = client.post(
        "/api/attachments",
        files={"file": ("image.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200), "image/png")},
    )
    assert upload.status_code == 201
    attachment_id = upload.json()["id"]

    model = _EchoModel()
    app.state.model = model
    response = client.post(
        f"/api/conversations/{conversation['id']}/stream",
        json={"message": "What is this image?", "attachment_ids": [attachment_id]},
    )
    assert response.status_code == 200
    assert "image.png" in model.seen_prompt
    assert "excerpt" not in model.seen_prompt
