from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import httpx


BASE_URL = "http://127.0.0.1:8120"
CASES = (
    ("A", "Generate an image of a futuristic automotive calibration shop", "comfyui-photorealistic"),
    ("B", "Generate a cinematic image of a modern ADAS calibration bay with vehicles, alignment targets, and holographic diagnostics", "comfyui-photorealistic"),
    ("C", "Generate a logo for Syfernetics", "xoduz-local-design"),
)


def run_case(client: httpx.Client, label: str, prompt: str, expected_provider: str) -> dict:
    conversation = client.post("/api/conversations", json={"title": f"ComfyUI acceptance {label}"})
    conversation.raise_for_status()
    conversation_id = conversation.json()["id"]
    answer: list[str] = []
    completed_calls: list[dict] = []
    started = time.perf_counter()
    with client.stream("POST", f"/api/conversations/{conversation_id}/stream", json={"message": prompt}, timeout=360) as response:
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
                    completed_calls.append(body)
                elif event == "error":
                    raise AssertionError(body.get("message") or "chat stream failed")
    generation = next((item for item in completed_calls if item.get("capability_id") == "media.image.generate"), None)
    if not generation:
        raise AssertionError(f"case {label}: model did not select media.image.generate: {completed_calls}")
    result = generation.get("result") or {}
    artifact = result.get("artifact") or {}
    if result.get("status") != "success" or result.get("provider") != expected_provider:
        raise AssertionError(f"case {label}: provider/result mismatch: {result}")
    if artifact.get("type") != "image" or not artifact.get("downloadable") or not artifact.get("reference"):
        raise AssertionError(f"case {label}: chat image artifact contract failed: {artifact}")
    downloaded = client.get(artifact["reference"], timeout=30)
    downloaded.raise_for_status()
    expected_mime = "image/svg+xml" if expected_provider == "xoduz-local-design" else "image/png"
    if artifact.get("mime_type") != expected_mime or len(downloaded.content) < 500:
        raise AssertionError(f"case {label}: artifact content was not a valid {expected_mime} result")
    stored = client.get(f"/api/conversations/{conversation_id}")
    stored.raise_for_status()
    messages = stored.json().get("messages") or []
    cards = ((messages[-1].get("metadata") or {}).get("capability_cards") or []) if messages else []
    if not any((card.get("result") or {}).get("artifact", {}).get("id") == artifact.get("id") for card in cards):
        raise AssertionError(f"case {label}: artifact was not persisted in the chat card metadata")
    return {
        "label": label,
        "prompt": prompt,
        "conversation_id": conversation_id,
        "provider": result["provider"],
        "selected_because": result.get("selected_because"),
        "fallback_used": result.get("fallback_used"),
        "artifact_id": artifact["id"],
        "mime_type": artifact["mime_type"],
        "size_bytes": len(downloaded.content),
        "sha256": artifact.get("sha256"),
        "checkpoint": (artifact.get("metadata") or {}).get("checkpoint"),
        "inline_chat_card_persisted": True,
        "view_download_verified": True,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "answer": "".join(answer).strip(),
    }


def main() -> int:
    with httpx.Client(base_url=BASE_URL, timeout=360) as client:
        login = client.post("/api/auth/test-login", json={"persona": "admin"})
        login.raise_for_status()
        health = client.get("/api/health").json()["services"]["creator"]
        image_health = health.get("image_provider_status") or {}
        if health.get("image_provider") != "comfyui-photorealistic" or not image_health.get("healthy"):
            raise AssertionError(f"XV12 API does not report a healthy ComfyUI image provider: {health}")
        results = [run_case(client, *case) for case in CASES]
    report = {
        "result": "PASS",
        "executed_at": datetime.now(UTC).isoformat(),
        "runtime": {
            "provider": image_health.get("provider"),
            "status": image_health.get("status"),
            "checkpoint": image_health.get("checkpoint"),
            "size": image_health.get("size"),
            "comfyui_version": image_health.get("comfyui_version"),
            "device": image_health.get("device"),
        },
        "cases": results,
    }
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
