from __future__ import annotations

from pathlib import Path
from typing import Any

from app.builder_execution import BuilderExecutionService
from app.creator import CreatorStore


def _counts(store: CreatorStore) -> tuple[int, int]:
    with store.connect() as db:
        sessions = int(db.execute("SELECT COUNT(*) FROM builder_execution_sessions").fetchone()[0])
        jobs = int(db.execute("SELECT COUNT(*) FROM creator_jobs").fetchone()[0])
    return sessions, jobs


def test_repeated_builder_execute_reuses_active_job_without_second_job_card_payload(tmp_path: Path) -> None:
    store = CreatorStore(tmp_path / "creator.sqlite", tmp_path / "workspaces")
    store.initialize()
    user: dict[str, Any] = {"id": "admin-user", "role": "admin", "status": "active"}
    conversation_id = "conversation-1"

    workspace = store.create_workspace(user["id"], "Tim's Taco Truck Website")
    session = store.create_builder_session(
        user_id=user["id"],
        conversation_id=conversation_id,
        workspace_id=str(workspace["id"]),
        request="Build a website for Tim's Taco Truck",
        mode="build",
    )
    job = store.create_job(
        user["id"],
        conversation_id,
        "builder.session.execute",
        str(workspace["id"]),
        {"builder_session_id": session["id"], "request": "Build a website for Tim's Taco Truck", "mode": "build"},
    )
    store.update_job(str(job["id"]), state="running", progress=28, message="Testing and validating application")
    store.update_builder_session(
        str(session["id"]),
        job_id=str(job["id"]),
        status="running",
        stage="Testing and validating application",
    )

    service = BuilderExecutionService(
        store=store,
        jobs=object(),
        workspaces=object(),
        previews=object(),
        artifacts=object(),
        model_provider=lambda: object(),
        registry=object(),
        gateway=object(),
    )

    before = _counts(store)
    result = service.execute(
        {
            "request": "Check the status of the website build for Tim's Taco Truck",
            "conversation_id": conversation_id,
            "mode": "build",
        },
        user,
    )
    after = _counts(store)

    assert result["status"] == "success"
    assert result["queued"] is True
    assert result["reused_existing_job"] is True
    assert result["active_job"]["job_id"] == job["id"]
    assert result["active_job"]["progress"] == 28
    assert result["workspace_id"] == workspace["id"]
    assert result["builder_session"]["id"] == session["id"]
    assert result["do_not_poll_in_this_turn"] is True
    assert "job" not in result  # the generic chat renderer must not create a duplicate progress card
    assert before == after  # no second Builder session and no second Creator job were created
