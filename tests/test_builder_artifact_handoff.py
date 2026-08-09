from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.artifacts import _user_message
from app.builder_execution import BuilderEvidence, BuilderExecutionService
from app.creator import CreatorStore
from .conftest import create_conversation, login


def call(client: TestClient, capability_id: str, **arguments: Any) -> dict[str, Any]:
    response = client.post(f"/api/capabilities/{capability_id}", json={"arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_recent_generated_image_is_staged_and_real_application_reference_is_detected(app, client):
    user = login(client, "admin")
    conversation = create_conversation(client)
    image = call(
        client,
        "media.image.generate",
        prompt="Tim's Taco Truck",
        provider="design",
        title="Tim's Taco Truck",
        conversation_id=conversation["id"],
    )
    workspace = call(client, "builder.workspace.create", name="Tim's Taco Truck Website")["workspace"]
    service = app.state.creator_platform.builder_execution
    assert service is not None

    staged = service._stage_conversation_assets(user["id"], conversation["id"], workspace["id"])
    assert staged
    assert staged[0]["artifact_id"] == image["artifact"]["id"]
    assert staged[0]["most_recent"] is True
    assert staged[0]["path"].startswith("assets/xv12/")

    root = app.state.creator_platform.store.safe_path(workspace["id"], user["id"], ".", must_exist=True)
    staged_path = root / staged[0]["path"]
    assert staged_path.is_file() and staged_path.stat().st_size > 0

    (root / "styles.css").write_text(
        ".hero{background-image:linear-gradient(#0006,#0006),url('./" + staged[0]["path"] + "');background-size:cover}",
        encoding="utf-8",
    )
    usage = service._scan_asset_usage(workspace["id"], user["id"], staged)
    assert usage[0]["referenced"] is True
    assert usage[0]["referenced_by"] == ["styles.css"]


def test_requirement_review_receives_asset_usage_evidence_and_rejects_unreferenced_requested_asset():
    class Reviewer:
        async def complete(self, messages, max_tokens=512):
            assert max_tokens == 512
            system = messages[0]["content"]
            prompt = messages[1]["content"]
            assert "matching staged asset must show referenced=true" in system
            assert "Tim's Taco Truck" in prompt
            assert '"referenced": false' in prompt
            return json.dumps({"satisfied": False, "missing": ["The requested Taco Truck image is not referenced by the application source."]})

    evidence = BuilderEvidence(
        workspace_id="workspace",
        files_changed=True,
        sandbox_succeeded=True,
        browser_healthy=True,
        browser_title="Tim's Taco Truck",
        browser_body_text="Menu About Contact",
        staged_assets=[{
            "artifact_id": "image-1", "title": "Tim's Taco Truck", "mime_type": "image/png",
            "path": "assets/xv12/artifact-image-1.png", "most_recent": True,
        }],
        asset_usage=[{
            "artifact_id": "image-1", "title": "Tim's Taco Truck", "path": "assets/xv12/artifact-image-1.png",
            "referenced_by": [], "referenced": False,
        }],
    )
    satisfied, missing = asyncio.run(BuilderExecutionService._review_requirements(
        Reviewer(),
        {"original_request": "Use this image as the background for the website."},
        evidence,
        [],
    ))
    assert satisfied is False
    assert "not referenced" in missing[0]


class _NoArtifacts:
    def recent_records(self, *_args, **_kwargs):
        return []


class _ImmediateJobs:
    def __init__(self, store: CreatorStore) -> None:
        self.store = store

    def submit(self, user_id, conversation_id, job_type, workspace_id, inputs, _worker):
        item = self.store.create_job(user_id, conversation_id, job_type, workspace_id, inputs)
        job_id = str(item["id"])
        self.store.update_job(
            job_id,
            state="succeeded",
            progress=100,
            message="Completed before the model attempted to poll it",
            result_json=json.dumps({"status": "success"}),
            completed_at="now",
        )
        return self.store.job_public(self.store.job(job_id, user_id) or item)


def test_same_user_turn_reuses_completed_builder_job_instead_of_starting_status_job(tmp_path: Path):
    store = CreatorStore(tmp_path / "creator.sqlite", tmp_path / "workspaces")
    store.initialize()
    jobs = _ImmediateJobs(store)
    service = BuilderExecutionService(
        store=store,
        jobs=jobs,
        workspaces=object(),
        previews=object(),
        artifacts=_NoArtifacts(),
        model_provider=lambda: object(),
        registry=object(),
        gateway=object(),
    )
    user = {"id": "admin-user", "role": "admin", "status": "active"}
    token = _user_message.set("build a website tims taco truck")
    try:
        first = service.execute(
            {"request": "Create a modern responsive website for Tim's Taco Truck", "conversation_id": "conversation", "mode": "build"},
            user,
        )
        assert "job" in first
        first_job_id = first["job"]["job_id"]
        with store.connect() as db:
            before_jobs = int(db.execute("SELECT COUNT(*) FROM creator_jobs").fetchone()[0])
            before_sessions = int(db.execute("SELECT COUNT(*) FROM builder_execution_sessions").fetchone()[0])

        second = service.execute(
            {"request": "Check the status of the website build for Tim's Taco Truck", "conversation_id": "conversation", "mode": "build"},
            user,
        )
        with store.connect() as db:
            after_jobs = int(db.execute("SELECT COUNT(*) FROM creator_jobs").fetchone()[0])
            after_sessions = int(db.execute("SELECT COUNT(*) FROM builder_execution_sessions").fetchone()[0])

        assert second["reused_existing_job"] is True
        assert second["active_job"]["job_id"] == first_job_id
        assert second["active_job"]["state"] == "succeeded"
        assert "job" not in second
        assert (before_jobs, before_sessions) == (after_jobs, after_sessions) == (1, 1)
    finally:
        _user_message.reset(token)
