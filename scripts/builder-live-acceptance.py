from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx


BASE_URL = "http://127.0.0.1:8120"
SALLY = "Generate a website for Sally's Seashells."
FOLLOW_UP = "Make the site feel more upscale, add a featured collection section, and change the accent color to sea-glass green."
SCHEDULING = "Build me a small mobile-first customer scheduling app and show it to me here."


def chat(client: httpx.Client, conversation_id: str, prompt: str) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    answer: list[str] = []
    with client.stream(
        "POST", f"/api/conversations/{conversation_id}/stream", json={"message": prompt}, timeout=360,
    ) as response:
        response.raise_for_status()
        event = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                body = json.loads(line[6:])
                if event == "delta":
                    answer.append(body.get("text", ""))
                elif event == "capability" and body.get("status") == "complete":
                    calls.append(body)
                elif event == "error":
                    raise AssertionError(body.get("message") or "chat stream failed")
    text = "".join(answer).strip()
    if "safe tool-call limit" in text:
        raise AssertionError("ordinary chat safe tool-call limit leaked into Builder execution")
    builder_calls = [item for item in calls if item.get("capability_id") == "builder.session.execute"]
    if len(builder_calls) != 1:
        raise AssertionError(f"expected one high-level Builder call, received {calls}")
    if any(str(item.get("capability_id") or "").startswith("builder.") and item not in builder_calls for item in calls):
        raise AssertionError(f"ordinary chat chained low-level Builder calls: {calls}")
    result = builder_calls[0].get("result") or {}
    if result.get("status") != "success" or not (result.get("job") or {}).get("job_id"):
        raise AssertionError(f"Builder session did not queue: {result}")
    return {"answer": text, "call": builder_calls[0], "job_id": result["job"]["job_id"],
            "workspace_id": result.get("workspace_id"), "session_id": (result.get("builder_session") or {}).get("id")}


def wait_job(client: httpx.Client, job_id: str, timeout: float = 1200) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/creator/jobs/{job_id}", timeout=30)
        response.raise_for_status()
        current = response.json()
        marker = (current.get("state"), current.get("progress"), current.get("message"))
        if marker != last:
            print(f"[{job_id[:8]}] {marker[0]} {marker[1]}% {marker[2]}", flush=True)
            last = marker
        if current.get("state") in {"succeeded", "failed", "cancelled"}:
            return current
        time.sleep(1.5)
    raise AssertionError(f"Builder job {job_id} did not finish in {timeout} seconds")


def verify_job(client: httpx.Client, job: dict[str, Any], expected_text: str) -> dict[str, Any]:
    if job.get("state") != "succeeded":
        raise AssertionError(f"Builder job did not succeed: {job}")
    result = job.get("result") or {}
    artifact = result.get("artifact") or {}
    metadata = artifact.get("metadata") or {}
    if result.get("status") != "success" or not (result.get("validation") or {}).get("healthy"):
        raise AssertionError(f"Builder validation did not prove success: {result}")
    if artifact.get("type") != "application" or not str(metadata.get("preview_url") or "").startswith("/api/creator/previews/"):
        raise AssertionError(f"live application artifact contract failed: {artifact}")
    if not metadata.get("screenshot") or not metadata.get("project_archive"):
        raise AssertionError("application artifact lacks screenshot fallback or project archive")
    preview = client.get(metadata["preview_url"], timeout=30)
    preview.raise_for_status()
    if expected_text.casefold() not in preview.text.casefold():
        raise AssertionError(f"live preview did not contain expected text: {expected_text}")
    archive = client.get(metadata["project_archive"]["reference"] + "?download=true", timeout=30)
    archive.raise_for_status()
    if len(archive.content) < 300:
        raise AssertionError("project archive download was empty")
    return {
        "state": job["state"], "job_id": job["job_id"], "workspace_id": result["workspace_id"],
        "session_id": result["builder_session"]["id"], "parent_session_id": result["builder_session"].get("parent_session_id"),
        "preview_id": result["preview_id"], "artifact_id": artifact["id"],
        "operations": result["operations_completed"], "model_rounds": result["model_rounds"],
        "repair_cycles": result["builder_session"]["repair_cycles"],
        "inline_proxy_verified": True, "download_project_verified": True,
        "screenshot_artifact_id": metadata["screenshot"]["id"],
    }


def run_case(client: httpx.Client, prompt: str, expected_text: str, conversation_id: str | None = None) -> tuple[str, dict[str, Any]]:
    if not conversation_id:
        conversation = client.post("/api/conversations", json={"title": "Builder live acceptance"})
        conversation.raise_for_status()
        conversation_id = conversation.json()["id"]
    started = time.perf_counter()
    turn = chat(client, conversation_id, prompt)
    job = wait_job(client, turn["job_id"])
    proof = verify_job(client, job, expected_text)
    proof.update({"prompt": prompt, "answer": turn["answer"], "duration_seconds": round(time.perf_counter() - started, 3)})
    return conversation_id, proof


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=360) as client:
        login = client.post("/api/auth/test-login", json={"persona": "admin"})
        login.raise_for_status()
        health = client.get("/api/health").json()
        if health["services"]["creator"].get("builder_execution") != "available":
            raise AssertionError("Builder execution service is unavailable")
        conversation_id, sally = run_case(client, SALLY, "Sally's Seashells")
        same_conversation, follow_up = run_case(client, FOLLOW_UP, "featured", conversation_id)
        if same_conversation != conversation_id or follow_up["workspace_id"] != sally["workspace_id"]:
            raise AssertionError("follow-up did not preserve the Sally workspace")
        if follow_up["preview_id"] != sally["preview_id"] or follow_up["artifact_id"] != sally["artifact_id"]:
            raise AssertionError("follow-up did not preserve the live preview/artifact identity")
        _, scheduling = run_case(client, SCHEDULING, "Book Your Appointment")
    report = {
        "result": "PASS", "executed_at": datetime.now(UTC).isoformat(),
        "sally": sally, "follow_up": follow_up, "scheduling": scheduling,
    }
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
