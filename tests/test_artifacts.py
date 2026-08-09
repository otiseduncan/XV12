from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT

from .conftest import create_conversation, login


pytestmark = pytest.mark.artifacts


def grant(client: TestClient, user_id: str, grants: list[dict]) -> None:
    response = client.put(f"/api/admin/capabilities/users/{user_id}/grants", json={"grants": grants})
    assert response.status_code == 200, response.text


def register_pdf(app, tmp_path: Path, user_id: str, conversation_id: str) -> tuple[dict, bytes]:
    source = tmp_path / "2018 Audi A5 electronics.pdf"
    body = b"%PDF-1.4\n% XV12 authorized OEM test document\n%%EOF\n"
    source.write_bytes(body)
    app.state.artifact_store.allowed_roots.append(tmp_path.resolve())
    artifact = app.state.artifact_store.register_file(
        user_id=user_id,
        conversation_id=conversation_id,
        capability_id="adas.si.search",
        source_path=source,
        title=source.name,
        source_label="ADAS SI",
        page=291,
        section="Lane Change Assistance calibration",
        relevant_text="Calibrate the lane change assistance control units with the authorized target fixture.",
        metadata={"internal_path": str(source), "query": "2018 Audi A5 lane change assist calibration"},
    )
    return artifact, body


def test_artifact_urls_are_user_scoped_and_permission_revocation_is_immediate(app, tmp_path: Path) -> None:
    with TestClient(app) as admin_client, TestClient(app) as owner_client, TestClient(app) as other_client:
        login(admin_client, "admin")
        owner = login(owner_client, "user-a")
        other = login(other_client, "user-b")
        conversation = create_conversation(owner_client)
        artifact, body = register_pdf(app, tmp_path, owner["id"], conversation["id"])

        public_json = json.dumps(artifact)
        assert str(tmp_path) not in public_json and "internal_path" not in public_json
        assert artifact["reference"] == f"/api/artifacts/{artifact['id']}/content"
        assert artifact["preview"]["page"] == 291
        assert owner_client.get(artifact["reference"]).status_code == 403

        grant(admin_client, owner["id"], [{"family": "adas_si", "scopes": ["read"]}])
        inline = owner_client.get(artifact["reference"])
        assert inline.status_code == 200 and inline.content == body
        assert inline.headers["content-type"].startswith("application/pdf")
        assert inline.headers["content-disposition"].startswith("inline")
        download = owner_client.get(f"{artifact['reference']}?download=true")
        assert download.headers["content-disposition"].startswith("attachment")
        copied = owner_client.get(f"/api/artifacts/{artifact['id']}/text")
        assert copied.status_code == 200 and "lane change assistance" in copied.text
        assert other_client.get(artifact["reference"]).status_code == 404

        grant(admin_client, owner["id"], [])
        assert owner_client.get(artifact["reference"]).status_code == 403


def test_display_followup_reuses_persisted_artifact_without_repeating_adas_search(app, tmp_path: Path) -> None:
    class RecentArtifactModel:
        tool_names: list[str] = []

        async def stream_events(self, messages, tools=None) -> AsyncIterator[dict]:
            if not any(item["role"] == "tool" for item in messages):
                names = {item["function"]["name"] for item in tools or []}
                assert "artifact_recent_read" in names
                assert "adas_si_search" not in names and "web_current_search" not in names
                yield {"type": "tool_call", "id": "recent-1", "name": "artifact_recent_read", "arguments": '{"action":"display","title":"2018 Audi A5 Lane Change Assist Calibration Procedure","limit":1}'}
            else:
                tool = next(item for item in messages if item["role"] == "tool")
                self.tool_names.append(tool["name"])
                yield {"type": "content", "text": "Here is the document you already retrieved."}

        async def health(self):
            return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

    with TestClient(app) as admin_client, TestClient(app) as user_client:
        login(admin_client, "admin")
        user = login(user_client, "user-a")
        grant(admin_client, user["id"], [{"family": "adas_si", "scopes": ["read"]}])
        conversation = create_conversation(user_client)
        artifact, _ = register_pdf(app, tmp_path, user["id"], conversation["id"])
        model = RecentArtifactModel()
        app.state.model = model

        response = user_client.post(
            f"/api/conversations/{conversation['id']}/stream",
            json={"message": "Display the document."},
        )
        assert response.status_code == 200
        assert '"reused_existing_reference": true' in response.text
        assert artifact["id"] in response.text
        assert "adas.si.search" not in response.text
        assert model.tool_names == ["artifact_recent_read"]

        stored = user_client.get(f"/api/conversations/{conversation['id']}").json()
        card = stored["messages"][-1]["metadata"]["capability_cards"][0]
        assert card["capability_id"] == "artifact.recent.read"
        assert card["result"]["artifacts"][0]["id"] == artifact["id"]


def test_generic_chat_renderer_covers_documents_images_tables_receipts_and_hides_tool_ids() -> None:
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    runtime = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
    assert runtime["versions"]["artifact_schema"] == 1
    assert "function appendArtifact(container, artifact)" in js
    for token in ('artifact.type === "image"', 'artifact.mime_type === "application/pdf"', 'artifact.type === "structured_data"', 'artifact.type === "receipt"'):
        assert token in js
    for action in ("View", "Download", "Print", "Copy"):
        assert f'textContent = "{action}"' in js or f'? "{action} text"' in js
    assert 'composerStatus.textContent = "Checking authorized sources…"' in js
    assert "textContent = card.capability_id" not in js
    assert 'document.execCommand("copy")' in js
    assert ".artifact-preview iframe" in css and "overflow-y:auto" in css
