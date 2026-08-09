from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def compact_capability(body: dict) -> dict:
    capability_id = body.get("capability_id", "")
    result = dict(body.get("result") or {})
    if capability_id == "calibration_iq.repair_orders.read":
        result = {key: result.get(key) for key in ("status", "count", "returned_count", "source_returned_count", "verification", "evidence")}
    elif capability_id == "adas.knowledge.search":
        result["results"] = [
            {
                "vehicle": item.get("vehicle"),
                "system": item.get("system"),
                "procedure": {key: (item.get("procedure") or {}).get(key) for key in ("title", "type", "calibration_type")},
                "requirements": item.get("requirements"),
                "source": item.get("source"),
            }
            for item in result.get("results", [])[:3]
        ]
    return {**body, "result": result}


def run_turn(client: httpx.Client, conversation_id: str, prompt: str) -> dict:
    started = time.perf_counter()
    first_delta = None
    text: list[str] = []
    capability_results: list[dict] = []
    events: list[str] = []
    with client.stream(
        "POST",
        f"/api/conversations/{conversation_id}/stream",
        json={"message": prompt, "attachment_ids": []},
    ) as response:
        response.raise_for_status()
        event = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                body = json.loads(line[6:])
                events.append(event)
                if event == "delta":
                    first_delta = first_delta or time.perf_counter()
                    text.append(body["text"])
                elif event == "capability" and body.get("status") == "complete":
                    capability_results.append(compact_capability(body))
                elif event == "error":
                    raise AssertionError(body.get("message"))
    return {
        "prompt": prompt,
        "first_token_seconds": round((first_delta - started), 3) if first_delta else None,
        "total_seconds": round(time.perf_counter() - started, 3),
        "events": sorted(set(events)),
        "capabilities": capability_results,
        "answer": "".join(text).strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8120")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    failures: list[str] = []
    with httpx.Client(base_url=args.base_url, timeout=240) as client:
        login = client.post("/api/auth/test-login", json={"persona": "admin"})
        login.raise_for_status()
        user = login.json()
        conversation = client.post("/api/conversations", json={"title": "Functional assistant live acceptance"}).json()
        prompts = [
            "Who am I?",
            "What local databases do you have access to?",
            "How many repair orders are currently in Calibration IQ?",
            "What verified calibration information do you have for a 2023 Hyundai Palisade front camera after windshield replacement?",
            "What's going on with the current conflict or skirmishes in the Middle East today? Use current evidence.",
            "Check your verified ADAS database for 2024 Rivian R1S front camera calibration. If it is absent, research the current public web rather than stopping at the local miss.",
        ]
        turns = [run_turn(client, conversation["id"], prompt) for prompt in prompts]
        if user.get("conversational_name") != "Otis":
            failures.append("admin conversational name was not Otis")
        if "Otis" not in turns[0]["answer"]:
            failures.append("identity answer did not recognize Otis")
        if "ADAS" not in turns[1]["answer"] or "Calibration IQ" not in turns[1]["answer"] or "System Health" in turns[1]["answer"]:
            failures.append("database awareness answer was incomplete")
        if "calibration_iq.repair_orders.read" not in {item["capability_id"] for item in turns[2]["capabilities"]}:
            failures.append("Calibration IQ capability was not model-selected")
        ciq_result = next((item["result"] for item in turns[2]["capabilities"] if item["capability_id"] == "calibration_iq.repair_orders.read"), {})
        if not isinstance(ciq_result.get("count"), int) or str(ciq_result["count"]) not in turns[2]["answer"]:
            failures.append("live Calibration IQ count was not synthesized")
        if "adas.knowledge.search" not in {item["capability_id"] for item in turns[3]["capabilities"]}:
            failures.append("ADAS capability was not model-selected")
        if "SPTAC" not in turns[3]["answer"] or "windshield" not in turns[3]["answer"].casefold():
            failures.append("verified ADAS procedure was not synthesized")
        current_cards = turns[4]["capabilities"]
        if "web.current.search" not in {item["capability_id"] for item in current_cards}:
            failures.append("live web capability was not model-selected")
        web_result = next((item["result"] for item in current_cards if item["capability_id"] == "web.current.search"), {})
        if not web_result.get("results") or not web_result.get("executed_at"):
            failures.append("live web evidence had no timestamp or sources")
        miss_ids = [item["capability_id"] for item in turns[5]["capabilities"]]
        if not any(item.startswith("adas.") for item in miss_ids) or "web.current.search" not in miss_ids:
            failures.append("database miss did not continue to the permitted web source")
        stored = client.get(f"/api/conversations/{conversation['id']}").json()
        if len(stored.get("messages", [])) != len(prompts) * 2:
            failures.append("production messages were not persisted")
    report = {
        "result": "PASS" if not failures else "FAIL",
        "executed_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "authenticated_user": {key: user[key] for key in ("id", "conversational_name", "role")},
        "turns": turns,
        "persisted_messages": len(stored.get("messages", [])),
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered.encode("ascii", errors="backslashreplace").decode("ascii"))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
