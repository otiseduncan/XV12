from __future__ import annotations

import json
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.artifacts import ArtifactStore, artifact_conversation
from app.capabilities.files import LocalFilesCapability
from app.config import ROOT

from .conftest import create_conversation, login


pytestmark = pytest.mark.artifacts


def grant(client: TestClient, user_id: str, grants: list[dict]) -> None:
    response = client.put(f"/api/admin/capabilities/users/{user_id}/grants", json={"grants": grants})
    assert response.status_code == 200, response.text


def register_pdf(app, tmp_path: Path, user_id: str, conversation_id: str) -> tuple[dict, bytes]:
    source = tmp_path / "2018 Audi A5 electronics.pdf"
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "2018 Audi A5 electronics"})
    with source.open("wb") as output:
        writer.write(output)
    body = source.read_bytes()
    app.state.artifact_store.allowed_roots.append(tmp_path.resolve())
    artifact = app.state.artifact_store.register_file(
        user_id=user_id,
        conversation_id=conversation_id,
        capability_id="adas.si.search",
        source_path=source,
        title="Lane Change Assistance — Calibration",
        source_title=source.name,
        source_label="ADAS SI",
        requested_scope="lane change assist calibration procedure",
        scope_kind="procedure",
        page_start=2,
        page_end=4,
        section_title="Lane Change Assistance",
        subsection_title="Calibration",
        section_page_start=1,
        section_page_end=5,
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
        assert artifact["preview"]["page"] == 1
        assert artifact["page_start"] == 2 and artifact["page_end"] == 4
        assert artifact["source_title"] == "2018 Audi A5 electronics.pdf"
        assert artifact["metadata"]["source_page_count"] == 6
        assert artifact["full_document_reference"].endswith("/full")
        assert owner_client.get(artifact["reference"]).status_code == 403

        grant(admin_client, owner["id"], [{"family": "adas_si", "scopes": ["read"]}])
        inline = owner_client.get(artifact["reference"])
        assert inline.status_code == 200 and len(PdfReader(BytesIO(inline.content)).pages) == 3
        assert inline.headers["content-type"].startswith("application/pdf")
        assert inline.headers["content-disposition"].startswith("inline")
        download = owner_client.get(f"{artifact['reference']}?download=true")
        assert download.headers["content-disposition"].startswith("attachment")
        assert "pages-2-4.pdf" in download.headers["content-disposition"]
        full = owner_client.get(artifact["full_document_reference"])
        assert full.status_code == 200 and full.content == body and len(PdfReader(BytesIO(full.content)).pages) == 6
        copied = owner_client.get(f"/api/artifacts/{artifact['id']}/text")
        assert copied.status_code == 200 and "lane change assistance" in copied.text
        assert other_client.get(artifact["reference"]).status_code == 404

        grant(admin_client, owner["id"], [])
        assert owner_client.get(artifact["reference"]).status_code == 403


def test_artifact_identity_deduplicates_the_same_source_range_and_section(app, tmp_path: Path) -> None:
    with TestClient(app) as client:
        user = login(client, "admin")
        conversation = create_conversation(client)
        first, _ = register_pdf(app, tmp_path, user["id"], conversation["id"])
        second, _ = register_pdf(app, tmp_path, user["id"], conversation["id"])
        assert first["id"] == second["id"] and first["display_key"] == second["display_key"]
        records = app.state.artifact_store.recent_records(user["id"], conversation["id"], "", 10)
        assert len(records) == 1


def test_local_pdf_capability_uses_the_generic_requested_page_scope(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=300, height=400)
    with source.open("wb") as output:
        writer.write(output)
    store = ArtifactStore(tmp_path / "artifacts.sqlite", [tmp_path])
    store.initialize()
    capability = LocalFilesCapability([tmp_path], tmp_path / "managed", store)
    user = {"id": "local-file-user", "role": "admin", "status": "active"}
    with artifact_conversation("local-file-scope"):
        result = capability.read({"path": str(source), "page_start": 2, "page_end": 3, "requested_scope": "summary tables"}, user)
    artifact = result["artifacts"][0]
    assert artifact["page_start"] == 2 and artifact["page_end"] == 3
    record = store.get_owned(artifact["id"], user["id"])
    assert record is not None and len(PdfReader(str(store.materialize(record))).pages) == 2


def test_display_followup_reuses_persisted_artifact_without_repeating_adas_search(app, tmp_path: Path) -> None:
    class RecentArtifactModel:
        tool_names: list[str] = []

        async def stream_events(self, messages, tools=None) -> AsyncIterator[dict]:
            if not any(item["role"] == "tool" for item in messages):
                names = {item["function"]["name"] for item in tools or []}
                assert "artifact_recent_read" in names
                assert "adas_si_search" not in names and "web_current_search" not in names
                yield {"type": "tool_call", "id": "recent-1", "name": "artifact_recent_read", "arguments": '{"action":"display","title":"2018 Audi A5 Lane Change Assist Calibration Procedure","page":2,"limit":1}'}
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
        assert '"capability_id": "adas.si.search"' not in response.text
        assert model.tool_names == ["artifact_recent_read"]

        stored = user_client.get(f"/api/conversations/{conversation['id']}").json()
        card = stored["messages"][-1]["metadata"]["capability_cards"][0]
        assert card["capability_id"] == "artifact.recent.read"
        assert card["result"]["artifacts"][0]["id"] == artifact["id"]


def test_page_section_and_full_document_followups_rescope_the_current_source(app, tmp_path: Path) -> None:
    class ScopeModel:
        capability_names: list[str] = []
        offered_tools: list[set[str]] = []

        async def stream_events(self, messages, tools=None) -> AsyncIterator[dict]:
            if not any(item["role"] == "tool" for item in messages):
                prompt = next(item["content"] for item in reversed(messages) if item["role"] == "user")
                names = {item["function"]["name"] for item in tools or []}
                self.offered_tools.append(names)
                arguments = '{"page":5}' if "page 5" in prompt.casefold() else '{"scope":"section"}' if "section" in prompt.casefold() else '{"scope":"full"}'
                yield {"type": "tool_call", "id": "scope-call", "name": "artifact_recent_read", "arguments": arguments}
            else:
                tool = next(item for item in messages if item["role"] == "tool")
                self.capability_names.append(tool["name"])
                yield {"type": "content", "text": "Displayed from the existing source."}

        async def health(self):
            return {"reachable": True, "alias_ok": True, "models": ["xoduz-qwen3-coder-30b"]}

    with TestClient(app) as client:
        user = login(client, "admin")
        conversation = create_conversation(client)
        register_pdf(app, tmp_path, user["id"], conversation["id"])
        model = ScopeModel()
        app.state.model = model
        expected = [("Display page 5.", "page", 5, 5), ("Show me the entire Lane Change Assist section.", "section", 1, 5), ("Show me the whole document.", "full", None, None)]
        for prompt, scope, start, end in expected:
            response = client.post(f"/api/conversations/{conversation['id']}/stream", json={"message": prompt})
            assert response.status_code == 200 and '"capability_id": "adas.si.search"' not in response.text
            assert "event: error" not in response.text, response.text
            stored = client.get(f"/api/conversations/{conversation['id']}").json()
            artifact = stored["messages"][-1]["metadata"]["capability_cards"][0]["result"]["artifacts"][0]
            assert artifact["metadata"]["scope_kind"] == scope
            assert (artifact["page_start"], artifact["page_end"]) == (start, end)
            if scope == "section":
                assert artifact["section_title"] == "Lane Change Assistance" and artifact["subsection_title"] is None
            if scope == "full":
                assert artifact["full_document_reference"] is None and artifact["copyable"] is False
        assert model.capability_names == ["artifact_recent_read"] * 3
        offered_state = [("artifact_recent_read" in names, "adas_si_search" in names) for names in model.offered_tools]
        assert offered_state == [(True, False)] * 3


def test_generic_chat_renderer_covers_documents_images_tables_receipts_and_hides_tool_ids() -> None:
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
    html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    runtime = json.loads((ROOT / "config" / "runtime.json").read_text(encoding="utf-8"))
    assert runtime["versions"]["artifact_schema"] == 3
    assert "/static/app.js?v=4.0.0" in html and "/static/styles.css?v=4.0.0" in html
    assert "function appendArtifact(container, artifact)" in js
    for token in ('artifact.type === "image"', 'artifact.mime_type === "application/pdf"', 'artifact.type === "structured_data"', 'artifact.type === "receipt"'):
        assert token in js
    assert 'artifact.type === "video"' in js and 'artifact.type === "application"' in js
    for action in ("View", "Download Section", "Print Section", "Copy text", "Full Document"):
        assert f'"{action}"' in js
    assert 'composerStatus.textContent = "Checking authorized sources…"' in js
    assert "textContent = card.capability_id" not in js
    assert "artifact.full_document_reference" in js and "artifact.display_key" in js
    assert 'duplicate.remove()' in js and '"Download Section"' in js and '"Print Section"' in js
    assert 'document.execCommand("copy")' in js
    assert '`${base}${base.includes("?") ? "&" : "?"}download=true`' in js
    assert ".artifact-preview iframe" in css and "overflow-y:auto" in css
