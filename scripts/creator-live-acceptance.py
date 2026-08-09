from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.artifacts import artifact_conversation
from app.config import Settings
from app.main import create_app


async def main() -> dict[str, Any]:
    settings = Settings.load()
    app = create_app(settings)
    admin = next(item for item in app.state.permission_store.list_users() if item["role"] == "admin")
    user = {"id": admin["id"], "role": "admin", "status": "active"}
    conversation = app.state.store.create_conversation(user["id"], "Creator Platform Live Acceptance")
    app.state.store.add_message(
        user["id"], conversation["id"], "user",
        "Build, test, repair, preview, validate, and package a customer scheduling application; then create image and video assets.",
    )
    cards: list[dict[str, Any]] = []

    async def call(capability_id: str, arguments: dict[str, Any], *, record: bool = True) -> dict[str, Any]:
        result, decision = await app.state.gateway.execute(capability_id, user, arguments)
        if record:
            cards.append({"capability_id": capability_id, "arguments": {key: value for key, value in arguments.items() if key not in {"files", "content"}}, "result": result})
        return result

    conversation_id = conversation["id"]
    with artifact_conversation(conversation_id):
        created = await call("builder.workspace.create", {"name": "Customer Scheduling Operations Center"})
        workspace_id = created["workspace"]["id"]
        index_without_accessibility_fix = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Customer Scheduling Operations Center</title><link rel="stylesheet" href="styles.css"></head>
<body><main><header><p class="eyebrow">XODUZ CREATOR ACCEPTANCE</p><h1>Customer Scheduling Operations Center</h1><p>Book, assign, and track every service appointment from one responsive workspace.</p></header>
<section class="metrics"><article><strong id="today-count">4</strong><span>Today</span></article><article><strong>92%</strong><span>On-time</span></article><article><strong>3</strong><span>Open bays</span></article></section>
<section class="layout"><form id="booking"><h2>New appointment</h2><label>Customer<input id="customer" required></label><label>Service<select id="service"><option>ADAS calibration</option><option>Diagnostic</option><option>Glass replacement</option></select></label><label>Date<input id="date" type="date" required></label><button id="book" type="submit">Schedule customer</button></form>
<div><h2>Upcoming schedule</h2><ol id="appointments"><li><time>09:00</time><span>Jordan Lee · ADAS calibration</span><b>Bay 2</b></li><li><time>11:30</time><span>Alex Morgan · Diagnostic</span><b>Bay 1</b></li></ol></div></section></main><script src="app.js"></script></body></html>"""
        styles = """*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 80% 10%,#174852 0,transparent 35%),#061217;color:#e7f8f9;font:16px system-ui}main{width:min(1100px,92vw);margin:6vh auto}.eyebrow{color:#5fe4ed;letter-spacing:.18em;font-size:.75rem}h1{font-size:clamp(2rem,5vw,4rem);max-width:800px;margin:.2em 0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:30px 0}.metrics article,form,.layout>div{background:#0b2229;border:1px solid #24515a;border-radius:18px;padding:24px}.metrics strong{display:block;font-size:2rem;color:#73edf4}.metrics span{color:#8fb0b5}.layout{display:grid;grid-template-columns:.8fr 1.2fr;gap:18px}form{display:grid;gap:14px}label{display:grid;gap:6px;color:#9fc0c4}input,select,button{width:100%;padding:12px;border-radius:9px;border:1px solid #315c63;background:#07171c;color:#e7f8f9}button{background:#57dce5;color:#031216;font-weight:700;cursor:pointer}ol{padding:0;list-style:none}li{display:grid;grid-template-columns:70px 1fr auto;gap:12px;padding:15px 0;border-bottom:1px solid #1e4148}time{color:#66dbe3}b{font-size:.8rem;color:#a7c7ca}@media(max-width:720px){.layout{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr 1fr}li{grid-template-columns:55px 1fr}li b{grid-column:2}}"""
        script = """const form=document.querySelector('#booking'),list=document.querySelector('#appointments'),count=document.querySelector('#today-count');form.addEventListener('submit',event=>{event.preventDefault();const customer=document.querySelector('#customer').value.trim(),service=document.querySelector('#service').value,date=document.querySelector('#date').value;if(!customer||!date)return;const item=document.createElement('li');item.innerHTML=`<time>${date.slice(5)}</time><span></span><b>Queued</b>`;item.querySelector('span').textContent=`${customer} · ${service}`;list.append(item);count.textContent=String(Number(count.textContent)+1);form.reset()});"""
        failing_test = """from pathlib import Path
def test_application_contract():
    page=Path('index.html').read_text()
    assert 'Customer Scheduling Operations Center' in page
    assert 'Schedule customer' in page
def test_accessible_booking_action():
    assert 'aria-label="Schedule customer appointment"' in Path('index.html').read_text()
"""
        await call("builder.files.batch", {"workspace_id": workspace_id, "files": [
            {"path": "index.html", "content": index_without_accessibility_fix}, {"path": "styles.css", "content": styles},
            {"path": "app.js", "content": script}, {"path": "test_app.py", "content": failing_test},
            {"path": "requirements-dev.txt", "content": "pytest==8.4.2\n"},
        ]})
        dependency = await call("builder.sandbox.exec", {
            "workspace_id": workspace_id, "argv": ["python", "-m", "pip", "install", "--disable-pip-version-check", "--target", ".creator-deps", "-r", "requirements-dev.txt"],
            "network": True, "timeout_seconds": 300, "report_type": "build_report", "conversation_id": conversation_id,
        })
        if dependency.get("exit_code") != 0:
            raise RuntimeError("Dependency installation failed")
        failed = await call("builder.sandbox.exec", {
            "workspace_id": workspace_id, "argv": ["sh", "-lc", "PYTHONPATH=.creator-deps python -m pytest -q"],
            "timeout_seconds": 120, "report_type": "test_report", "conversation_id": conversation_id,
        })
        if failed.get("exit_code") == 0:
            raise RuntimeError("Deliberate negative control did not fail")
        repaired_index = index_without_accessibility_fix.replace('id="book" type="submit"', 'id="book" type="submit" aria-label="Schedule customer appointment"')
        await call("builder.files.patch", {"workspace_id": workspace_id, "path": "index.html", "content": repaired_index})
        passed = await call("builder.sandbox.exec", {
            "workspace_id": workspace_id, "argv": ["sh", "-lc", "PYTHONPATH=.creator-deps python -m pytest -q"],
            "timeout_seconds": 120, "report_type": "test_report", "conversation_id": conversation_id,
        })
        if passed.get("exit_code") != 0:
            raise RuntimeError("Repaired tests failed")
        preview = await call("builder.preview.start", {"workspace_id": workspace_id, "title": "Customer Scheduling Operations Center", "conversation_id": conversation_id})
        preview_id = preview["preview"]["id"]
        inspected = await call("browser.preview.inspect", {"preview_id": preview_id})
        if not inspected.get("rendered"):
            raise RuntimeError("Browser inspection failed")
        screenshot = await call("browser.preview.screenshot", {"preview_id": preview_id, "title": "Customer scheduling application", "conversation_id": conversation_id})
        archive = await call("builder.project.archive", {"workspace_id": workspace_id, "conversation_id": conversation_id})
        image = await call("media.image.generate", {"prompt": "Premium cinematic customer scheduling operations command center with luminous teal status panels", "title": "Scheduling campaign visual", "width": 1280, "height": 720, "conversation_id": conversation_id})
        edited = await call("media.image.edit", {"source_artifact_id": image["artifact"]["id"], "prompt": "Refine the campaign visual with warm amber priority accents and premium depth", "title": "Scheduling campaign visual - amber edit", "conversation_id": conversation_id})
        queued = await call("media.video.generate", {"source_artifact_id": edited["artifact"]["id"], "prompt": "Ten second cinematic slow push through the scheduling command center", "title": "Scheduling campaign cinematic", "duration_seconds": 10, "conversation_id": conversation_id})
        job_id, deadline = queued["job"]["job_id"], time.monotonic() + 240
        current = app.state.creator_platform.store.job(job_id, user["id"])
        while current and current["state"] not in {"succeeded", "failed", "cancelled"} and time.monotonic() < deadline:
            await asyncio.sleep(1)
            current = app.state.creator_platform.store.job(job_id, user["id"])
        if not current or current["state"] != "succeeded":
            raise RuntimeError("Ten-second video job did not complete")
        job_result = app.state.creator_platform.store.job_public(current)
        cards.append({"capability_id": "job.status", "arguments": {"job_id": job_id}, "result": {"status": "success", "job": job_result}})

    app.state.store.add_message(
        user["id"], conversation_id, "assistant",
        "Creator acceptance complete: I built the scheduling application, installed its test dependency, proved the negative control, repaired accessibility, reran passing tests, rendered the live preview in Chromium, and attached the screenshot, project archive, source-linked images, and playable 10-second video.",
        "complete", {"capability_cards": cards},
    )
    return {
        "result": "PASS", "conversation_id": conversation_id, "workspace_id": workspace_id,
        "preview_id": preview_id, "preview_url": preview["preview"]["url"],
        "negative_control_exit_code": failed["exit_code"], "repaired_test_exit_code": passed["exit_code"],
        "browser_rendered": inspected["rendered"], "screenshot_artifact_id": screenshot["artifact"]["id"],
        "archive_artifact_id": archive["artifact"]["id"], "image_artifact_id": image["artifact"]["id"],
        "edited_image_artifact_id": edited["artifact"]["id"], "video_job_id": job_id,
        "video_artifact_id": job_result["result"]["artifact"]["id"], "video_duration_seconds": 10,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main()), indent=2))
