from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.capabilities.adas_inventory import AdasSourceInventory
from app.registry import CapabilityGateway, CapabilityRegistry, TRUTH_CONTRACT


class _AllowAllPermissions:
    def allows(self, _user_id: str, _family: str, _scope: str) -> bool:
        return True


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% inventory fixture only\n")


def test_adas_source_inventory_separates_documents_from_vehicle_applications(tmp_path: Path):
    source = tmp_path / "ADAS SI"
    _touch(source / "2018 Audi A5 electronics.pdf")
    _touch(source / "2018 Audi A5 communication.pdf")
    _touch(source / "2021 Buick Envision ACC.pdf")
    _touch(source / "2021 Buick Envision AWD LKAS.pdf")
    _touch(source / "2021 Buick Envision Parking Assist Sensor.pdf")
    _touch(source / "General calibration tooling.pdf")

    result = AdasSourceInventory(source).snapshot()

    assert result["status"] == "success"
    assert result["summary"]["document_count"] == 6
    assert result["summary"]["vehicle_application_count"] == 2
    assert result["summary"]["unparsed_document_count"] == 1
    assert result["entity_semantics"]["counts_are_not_interchangeable"] is True
    assert {(item["year"], item["make"], item["model"]) for item in result["applications"]} == {
        (2018, "Audi", "A5"),
        (2021, "Buick", "Envision"),
    }
    audi = next(item for item in result["applications"] if item["make"] == "Audi")
    assert audi["document_count"] == 2
    assert all(title.startswith("2018 Audi A5") for title in audi["source_documents"])


def test_adas_source_inventory_preserves_operator_verification_semantics(tmp_path: Path):
    source = tmp_path / "ADAS SI"
    _touch(source / "2018 Audi A5 electronics.pdf")

    result = AdasSourceInventory(source).snapshot()

    assert result["verification"]["source_library_status"] == "operator_verified"
    assert result["verification"]["verified_by"] == "Otis"
    assert result["verification"]["pipeline_metrics_are_separate"] is True
    assert result["evidence_contract"]["do_not_infer_records_from_counts"] is True


def test_gateway_attaches_truth_contract_to_every_capability_result(tmp_path: Path):
    registry_path = tmp_path / "capabilities.json"
    registry_path.write_text(
        json.dumps(
            {
                "registry_version": "test",
                "capabilities": [
                    {
                        "id": "inventory.test.read",
                        "family": "inventory",
                        "description": "Return inventory counts.",
                        "version": "1.0.0",
                        "risk_tier": 0,
                        "authorization": {"roles": ["admin", "user"]},
                        "arguments_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                        "result_schema": {"type": "object"},
                        "health": "available",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = CapabilityRegistry(registry_path, _AllowAllPermissions())
    gateway = CapabilityGateway(registry)
    gateway.register("inventory.test.read", lambda _: {"status": "success", "count": 10})
    user = {"id": "u-1", "role": "admin", "status": "active"}

    result, _decision = asyncio.run(gateway.execute("inventory.test.read", user, {}))

    assert result["evidence_contract"]["specific_records_must_be_present_in_result"] is True
    assert result["evidence_contract"]["counts_do_not_imply_missing_rows"] is True
    description = registry.model_tools(user)[0]["function"]["description"]
    assert TRUTH_CONTRACT in description


def test_registry_rejects_duplicate_capability_ids(tmp_path: Path):
    duplicate = {
        "id": "same.read",
        "family": "same",
        "description": "duplicate",
        "version": "1.0.0",
        "risk_tier": 0,
        "authorization": {"roles": ["admin"]},
        "arguments_schema": {"type": "object", "properties": {}},
        "result_schema": {"type": "object"},
        "health": "available",
    }
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps({"registry_version": "test", "capabilities": [duplicate, duplicate]}), encoding="utf-8")

    try:
        CapabilityRegistry(path)
    except ValueError as error:
        assert "Duplicate capability id" in str(error)
    else:
        raise AssertionError("duplicate capability ids must be rejected")
