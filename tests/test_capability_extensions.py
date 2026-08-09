from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pypdf import PdfReader

from app.capabilities.adas_si import AdasSICapability
from app.capabilities.calibration_iq import CalibrationIQCapability
from app.capabilities.files import LocalFilesCapability
from app.artifacts import ArtifactStore, artifact_conversation
from app.config import Settings
from app.registry import CapabilityGateway, CapabilityRegistry


ADMIN = {"id": "admin-test", "role": "admin", "status": "active"}


@pytest.mark.files
def test_local_files_read_write_modify_are_bounded_and_receipted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    managed = tmp_path / "managed"
    source.mkdir()
    (source / "read.txt").write_text("authorized evidence", encoding="utf-8")
    capability = LocalFilesCapability([source], managed)
    read = capability.read({"path": str(source / "read.txt")}, ADMIN)
    assert read["status"] == "success" and read["content"] == "authorized evidence"
    written = capability.write({"path": "acceptance/note.txt", "content": "one"}, ADMIN)
    assert written["receipt"]["user_id"] == ADMIN["id"]
    modified = capability.modify({"path": "acceptance/note.txt", "content": "two", "expected_sha256": written["receipt"]["sha256"]}, ADMIN)
    assert modified["receipt"]["before_sha256"] == written["receipt"]["sha256"]
    with pytest.raises(ValueError):
        capability.write({"path": str(source / "forbidden.txt"), "content": "no"}, ADMIN)


@pytest.mark.adas
def test_adas_managed_write_modify_never_changes_originals(tmp_path: Path) -> None:
    source = tmp_path / "adas"
    source.mkdir()
    original = source / "OEM-original.pdf"
    original.write_bytes(b"immutable-oem-evidence")
    before = original.read_bytes()
    capability = AdasSICapability(source, tmp_path / "cache" / "index.sqlite")
    created = capability.write({"record_id": "test-record", "title": "Test", "content": "v1"}, ADMIN)
    assert created["receipt"]["originals_modified"] is False
    changed = capability.modify({"record_id": "test-record", "expected_version": 1, "content": "v2"}, ADMIN)
    assert changed["receipt"]["version"] == 2
    assert original.read_bytes() == before


@pytest.mark.adas
def test_real_audi_a5_lane_change_search_returns_page_evidence(tmp_path: Path) -> None:
    source = Path(r"X:\ADAS SI")
    if not source.is_dir():
        pytest.skip("Authorized ADAS SI source is unavailable")
    artifact_store = ArtifactStore(tmp_path / "artifacts.sqlite", [source])
    artifact_store.initialize()
    capability = AdasSICapability(source, tmp_path / "adas-cache.sqlite", artifact_store)
    with artifact_conversation("audi-acceptance"):
        result = capability.search({"query": "lane change assist calibration procedure 2018 Audi A5"}, ADMIN)
    evidence = [item for item in result["results"] if item.get("excerpt")]
    assert result["status"] == "success"
    assert any("2018 Audi A5 electronics.pdf" in item["source"] for item in evidence)
    assert any(291 <= item["page"] <= 298 for item in evidence)
    assert result["broader_search_performed"] is True
    assert result["artifacts"][0]["title"] == "Lane Change Assistance — Calibration"
    assert result["artifacts"][0]["source"] == "ADAS SI"
    assert result["artifacts"][0]["source_title"] == "2018 Audi A5 electronics.pdf"
    assert (result["artifacts"][0]["page_start"], result["artifacts"][0]["page_end"]) == (290, 298)
    assert result["artifacts"][0]["metadata"]["section_page_start"] == 289
    assert result["artifacts"][0]["metadata"]["section_page_end"] == 298
    record = artifact_store.get_owned(result["artifacts"][0]["id"], ADMIN["id"])
    assert record is not None and len(PdfReader(str(artifact_store.materialize(record))).pages) == 9
    assert len(PdfReader(str(artifact_store.materialize(record, full=True))).pages) == 363
    assert str(source) not in str(result)


@pytest.mark.gateway
@pytest.mark.registry_gateway
def test_capability_failure_is_isolated_and_standardized() -> None:
    registry = CapabilityRegistry(Path("config/capabilities.v1.json"))
    gateway = CapabilityGateway(registry)
    gateway.register("system.health.read", lambda _arguments: (_ for _ in ()).throw(RuntimeError("boom")))
    result, _ = asyncio.run(gateway.execute("system.health.read", ADMIN, {}))
    assert result == {"status": "execution_error", "error": "RuntimeError", "message": "Capability execution failed safely."}


@pytest.mark.calibration_iq
def test_calibration_iq_mutation_receipt_and_idempotency_contract(monkeypatch, tmp_path: Path) -> None:
    class Response:
        status_code = 200
        def raise_for_status(self): return None
        def json(self):
            return {"success": True, "duplicate": True, "receipt": {"status": "completed", "mutation_id": "mutation-1", "operation": "update_ro", "ro": {"version": 3}}}

    class Client:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs): return Response()

    settings = Settings(
        root=Path.cwd(), app_host="127.0.0.1", app_port=8120, model_port=8121,
        model_alias="test", model_context_tokens=32768, model_max_tokens=100,
        model_temperature=.3, database_path=tmp_path / "db.sqlite", attachments_path=tmp_path,
        auth_mode="test", google_client_id="", google_client_secret="", google_redirect_uri="",
        owner_google_sub="owner", cookie_secure=False, calibration_iq_project_path=tmp_path,
    )
    (tmp_path / ".env").write_text("TOOL_SERVICE_TOKEN=test-token\n", encoding="utf-8")
    monkeypatch.setattr("app.capabilities.calibration_iq.httpx.AsyncClient", Client)
    capability = CalibrationIQCapability(settings)
    result = asyncio.run(capability.modify({
        "repair_order_id": "ro-1", "operation": "update_ro", "arguments": {"changes": {"initial_notes": "test"}},
        "expected_version": 2, "idempotency_key": "test-idempotency-0001", "correlation_id": "test-correlation",
    }, ADMIN))
    assert result["status"] == "success" and result["duplicate"] is True
    assert result["receipt"]["verified"] is True
    assert result["receipt"]["authenticated_user"] == ADMIN["id"]
