from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = Path(r"X:\calibration iq")


def main() -> int:
    subprocess.run(["docker", "compose", "stop"], cwd=SERVICE_ROOT, shell=False, check=True, timeout=90, capture_output=True)
    with httpx.Client(base_url="http://127.0.0.1:8120", timeout=240) as client:
        login = client.post("/api/auth/test-login", json={"persona": "admin"})
        login.raise_for_status()
        conversation = client.post("/api/conversations", json={"title": "Allowlisted service start acceptance"}).json()
        prompt = "Calibration IQ is offline. Start Calibration IQ now and verify that it is healthy."
        capability = None
        answer: list[str] = []
        started = time.perf_counter()
        with client.stream("POST", f"/api/conversations/{conversation['id']}/stream", json={"message": prompt}) as response:
            response.raise_for_status()
            event = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    body = json.loads(line[6:])
                    if event == "delta":
                        answer.append(body["text"])
                    elif event == "capability" and body.get("status") == "complete":
                        capability = body
                    elif event == "error":
                        raise AssertionError(body["message"])
        health = client.get("/api/health").json()["services"]["calibration_iq"]
    failures = []
    if not capability or capability.get("capability_id") != "service.calibration_iq.start":
        failures.append("model did not select the allowlisted start capability")
    result = (capability or {}).get("result") or {}
    if result.get("status") != "success" or result.get("domain_status") != "started" or result.get("executed") is not True:
        failures.append("start receipt did not prove execution")
    if health.get("status") != "available":
        failures.append("Calibration IQ did not become healthy")
    report = {
        "result": "PASS" if not failures else "FAIL",
        "executed_at": datetime.now(UTC).isoformat(),
        "prompt": prompt,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "capability": capability,
        "health": health,
        "answer": "".join(answer),
        "failures": failures,
    }
    output = ROOT / "docs" / "evidence" / "service-start-acceptance.json"
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
