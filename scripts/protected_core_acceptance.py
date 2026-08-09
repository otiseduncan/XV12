from __future__ import annotations

import json
import time
from typing import Any

import httpx


BASE_URL = "http://127.0.0.1:8120"
PROMPTS = [
    "Good morning X.",
    "We are planning Project Northstar, a calm service assistant. Remember its name and mood.",
    "What project name and mood did I give you?",
    "Who are you?",
]


def run() -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "result": "FAIL",
        "protected_sha": "1b94bbdd58afb973456f78a6a7bc412906caf13e",
        "turns": [],
    }
    with httpx.Client(base_url=BASE_URL, timeout=httpx.Timeout(15, read=300)) as client:
        login = client.post("/api/auth/test-login", json={"persona": "admin"})
        login.raise_for_status()
        conversation = client.post("/api/conversations", json={"title": "Protected core acceptance"})
        conversation.raise_for_status()
        conversation_id = conversation.json()["id"]
        for prompt in PROMPTS:
            started = time.perf_counter()
            first_token: float | None = None
            text: list[str] = []
            events: list[str] = []
            with client.stream(
                "POST",
                f"/api/conversations/{conversation_id}/stream",
                json={"message": prompt, "attachment_ids": []},
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        events.append(line[7:])
                    elif line.startswith("data: ") and events and events[-1] == "delta":
                        if first_token is None:
                            first_token = time.perf_counter()
                        text.append(json.loads(line[6:])["text"])
            ended = time.perf_counter()
            turn = {
                "prompt": prompt,
                "first_token_seconds": round((first_token or ended) - started, 3),
                "total_seconds": round(ended - started, 3),
                "events": sorted(set(events)),
                "response": "".join(text),
            }
            evidence["turns"].append(turn)

        persisted = client.get(f"/api/conversations/{conversation_id}")
        persisted.raise_for_status()
        evidence["persisted_messages"] = len(persisted.json()["messages"])

    failures: list[str] = []
    for turn in evidence["turns"]:
        if turn["first_token_seconds"] > 5:
            failures.append(f"first token exceeded 5 seconds: {turn['prompt']}")
        if turn["total_seconds"] > 20:
            failures.append(f"turn exceeded 20 seconds: {turn['prompt']}")
        if not {"meta", "delta", "done"}.issubset(turn["events"]):
            failures.append(f"stream contract incomplete: {turn['prompt']}")
    continuity = evidence["turns"][2]["response"].casefold()
    identity = evidence["turns"][3]["response"].casefold()
    if "northstar" not in continuity or "calm" not in continuity:
        failures.append("multi-turn continuity was not preserved")
    if "xoduz" not in identity and "xv12" not in identity:
        failures.append("X identity was not preserved")
    if evidence["persisted_messages"] != 8:
        failures.append("message persistence count was not eight")
    evidence["failures"] = failures
    evidence["result"] = "PASS" if not failures else "FAIL"
    return evidence


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["result"] == "PASS" else 1)
