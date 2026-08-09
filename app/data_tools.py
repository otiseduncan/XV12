from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import httpx

from .config import Settings


def _connect_adas(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def adas_coverage(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    path = settings.adas_database_path or settings.root / "data/knowledge/adas_knowledge.sqlite"
    if not path.exists():
        return {"status": "offline", "message": "XV12 ADAS knowledge database is unavailable."}
    make = str(arguments.get("make") or "").strip()
    model = str(arguments.get("model") or "").strip()
    year = arguments.get("year")
    clauses, params = ["va.active=1"], []
    if make:
        clauses.append("lower(m.name)=lower(?)")
        params.append(make)
    if model:
        clauses.append("lower(vm.name) LIKE lower(?)")
        params.append(f"%{model}%")
    if year:
        clauses.extend(["(va.year_start IS NULL OR va.year_start<=?)", "(va.year_end IS NULL OR va.year_end>=?)"])
        params.extend([int(year), int(year)])
    with _connect_adas(path) as db:
        applications = [
            dict(row)
            for row in db.execute(
                f"""SELECT va.id,va.year_start,va.year_end,va.trim,va.engine,va.applicability_notes,
                           m.name AS make,vm.name AS model
                    FROM vehicle_applications va
                    JOIN vehicle_models vm ON vm.id=va.vehicle_model_id
                    JOIN manufacturers m ON m.id=vm.manufacturer_id
                    WHERE {' AND '.join(clauses)} ORDER BY m.name,vm.name,va.year_start""",
                params,
            )
        ]
        counts = {
            "documents": int(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
            "verified_records": int(db.execute("SELECT COUNT(*) FROM verification_records WHERE active=1").fetchone()[0]),
            "review_items": int(db.execute("SELECT COUNT(*) FROM review_items WHERE status='needs_review'").fetchone()[0]),
            "active_source_documents": int(db.execute("SELECT COUNT(*) FROM source_documents WHERE active=1").fetchone()[0]),
        }
    return {
        "status": "verified" if applications else "no_result",
        "filters": {"make": make or None, "model": model or None, "year": year},
        "coverage": counts,
        "applications": applications,
        "evidence": {"source": "xv12_owned_adas_database", "read_only": True},
    }


def adas_search(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    path = settings.adas_database_path or settings.root / "data/knowledge/adas_knowledge.sqlite"
    if not path.exists():
        return {"status": "offline", "results": []}
    query = str(arguments.get("query") or "").strip()
    tokens = [part.casefold() for part in query.replace("-", " ").split() if len(part) >= 2][:12]
    if not tokens:
        return {"status": "invalid_request", "results": []}
    year_filter = arguments.get("year")
    make_filter = str(arguments.get("make") or "").strip()
    model_filter = str(arguments.get("model") or "").strip()
    explicit_vehicle = re.search(r"\b((?:19|20)\d{2})\s+([A-Za-z][A-Za-z.-]*)\s+([A-Za-z0-9][A-Za-z0-9 -]{0,30}?)(?=\s+(?:front|rear|camera|radar|windshield|adas|calibration)\b|$)", query, flags=re.I)
    if explicit_vehicle:
        year_filter = year_filter or int(explicit_vehicle.group(1))
        make_filter = make_filter or explicit_vehicle.group(2)
        model_filter = model_filter or explicit_vehicle.group(3).strip()
    with _connect_adas(path) as db:
        rows = db.execute(
            """SELECT p.id,p.title,p.procedure_type,p.calibration_type,p.details_json,
                      va.year_start,va.year_end,va.engine,m.name AS make,vm.name AS model,
                      ads.name AS system,sd.title AS source_title,sd.publication_date,
                      sc.page_number,sc.section_heading,sc.source_text
               FROM procedures p
               JOIN vehicle_applications va ON va.id=p.vehicle_application_id AND va.active=1
               JOIN vehicle_models vm ON vm.id=va.vehicle_model_id
               JOIN manufacturers m ON m.id=vm.manufacturer_id
               JOIN adas_systems ads ON ads.id=p.adas_system_id
               LEFT JOIN source_citations sc ON sc.record_type='procedure' AND sc.record_id=p.id
               LEFT JOIN source_documents sd ON sd.id=sc.source_document_id AND sd.active=1
               WHERE p.active=1"""
        ).fetchall()
        results = []
        ignored = {"the", "for", "what", "does", "after", "need", "have", "with", "verified", "information", "calibration", "about", "from"}
        significant = [token for token in tokens if token not in ignored]
        for row in rows:
            if year_filter and not (int(row["year_start"] or year_filter) <= int(year_filter) <= int(row["year_end"] or year_filter)):
                continue
            if make_filter and row["make"].casefold() != make_filter.casefold():
                continue
            if model_filter and model_filter.casefold() not in row["model"].casefold():
                continue
            details = json.loads(row["details_json"] or "{}")
            requirements = [
                dict(item)
                for item in db.execute(
                    """SELECT cr.required_status,cr.conditional_logic,cr.calibration_type,
                              re.normalized_trigger,co.name AS component
                       FROM calibration_requirements cr
                       LEFT JOIN repair_events re ON re.id=cr.repair_event_id
                       LEFT JOIN components co ON co.id=cr.component_id
                       WHERE cr.procedure_id=? AND cr.active=1""",
                    (row["id"],),
                )
            ]
            haystack = " ".join(str(value) for value in [dict(row), details, requirements]).casefold()
            score = sum(token in haystack for token in significant)
            if score < min(3, len(significant)):
                continue
            results.append(
                {
                    "vehicle": {"year_start": row["year_start"], "year_end": row["year_end"], "make": row["make"], "model": row["model"], "engine": row["engine"]},
                    "system": row["system"],
                    "procedure": {"title": row["title"], "type": row["procedure_type"], "calibration_type": row["calibration_type"], "details": details},
                    "requirements": requirements,
                    "source": {"title": row["source_title"], "publication_date": row["publication_date"], "page": row["page_number"], "section": row["section_heading"], "excerpt": row["source_text"]},
                }
            )
    return {
        "status": "verified" if results else "no_result",
        "query": query,
        "results": results[:5],
        "evidence": {"source": "xv12_owned_adas_database", "verified_only": True, "read_only": True},
    }


def _calibration_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = path / ".env"
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    return values


async def calibration_iq_read(settings: Settings, arguments: dict[str, Any]) -> dict[str, Any]:
    token = _calibration_env(settings.calibration_iq_project_path).get("TOOL_SERVICE_TOKEN", "")
    if not token:
        return {"status": "not_configured", "items": []}
    allowed = {key: arguments[key] for key in ("q", "shop", "insurance", "status", "phase", "limit", "offset") if arguments.get(key) not in (None, "")}
    allowed["limit"] = min(max(int(allowed.get("limit", 20)), 1), 100)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{settings.calibration_iq_base_url}/collection/ros",
                params=allowed,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code == 401:
            return {"status": "authentication_failed", "items": []}
        response.raise_for_status()
        body = response.json()
        source_returned = int(body.get("returned_count") or 0)
        body["items"] = list(body.get("items") or [])[:20]
        body["source_returned_count"] = source_returned
        body["returned_count"] = len(body["items"])
        if isinstance(body.get("verification"), dict):
            body["verification"]["complete"] = bool(body.get("offset", 0) + body["returned_count"] >= body.get("count", 0))
        return {"status": "verified", **body, "evidence": {"source": "calibration_iq_authenticated_api", "read_only": True}}
    except Exception as error:
        return {"status": "offline", "items": [], "error": type(error).__name__, "start_available_to_admin": True}


async def calibration_iq_health(settings: Settings) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.get(f"{settings.calibration_iq_base_url}/health")
        return {"status": "available" if response.status_code in {200, 401} else "degraded", "http_status": response.status_code}
    except Exception as error:
        return {"status": "offline", "error": type(error).__name__}


async def start_calibration_iq(settings: Settings, _: dict[str, Any]) -> dict[str, Any]:
    project = settings.calibration_iq_project_path.resolve()
    compose = project / "docker-compose.yml"
    if not compose.is_file():
        return {"status": "not_configured", "executed": False}
    completed = subprocess.run(
        ["docker", "compose", "up", "-d"],
        cwd=project,
        shell=False,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    health = await calibration_iq_health(settings)
    return {
        "status": "started" if completed.returncode == 0 and health["status"] == "available" else "failed",
        "executed": True,
        "command": "docker compose up -d",
        "exit_code": completed.returncode,
        "health": health,
    }
