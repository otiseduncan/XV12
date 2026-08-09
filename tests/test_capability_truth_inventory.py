from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.capabilities.adas_inventory import AdasSourceInventory
from app.capabilities.adas_si import AdasSICapability
from app.config import Settings
from app.data_tools import adas_coverage
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


def test_adas_source_inventory_canonicalizes_vehicle_identity_and_keeps_topics_as_documents(tmp_path: Path):
    source = tmp_path / "ADAS SI"
    for name in (
        "2026 Ford Truck F 150 4WD CCM.pdf",
        "2026 Ford Truck F 150 4WD SODCM.pdf",
        "2026 Ford Truck F 150 4WD SODCMC.pdf",
        "2022 Lexus ES 350 FWD BSM.pdf",
        "2022 Lexus ES 350 FWD panoramic.pdf",
        "2025 FORESTER BSM.pdf",
        "2025 FORESTER Eyesight.pdf",
        "2026 Kia K5 AWD (DL3) LKAS.pdf",
        "2026 Kia K5 AWD (DL3) SCC.pdf",
        "2018 Chevy Truck Tahoe 4WD.pdf",
        "2018 Chevy Truck Tahoe SAS.pdf",
    ):
        _touch(source / name)

    result = AdasSourceInventory(source).snapshot()
    vehicles = {(item["year"], item["make"], item["model"]) for item in result["applications"]}

    assert vehicles == {
        (2018, "Chevrolet", "Tahoe"),
        (2022, "Lexus", "ES 350"),
        (2025, "Subaru", "Forester"),
        (2026, "Ford", "F-150"),
        (2026, "Kia", "K5"),
    }
    ford = next(item for item in result["applications"] if item["year"] == 2026 and item["make"] == "Ford")
    assert ford["document_count"] == 3
    assert ford["drivetrains"] == ["4WD"]
    assert {topic.casefold() for topic in ford["topics"]} == {"ccm", "sodcm", "sodcmc"}
    assert "CCM" not in ford["model"]


def test_exact_source_resolution_prefers_requested_f150_ccm_document(tmp_path: Path):
    source = tmp_path / "ADAS SI"
    for name in (
        "2026 Ford Truck F 150 4WD CCM.pdf",
        "2026 Ford Truck F 150 4WD SODCM.pdf",
        "2026 Ford Truck F 150 4WD 360.pdf",
        "2024 Ford Truck Maverick FWD CCM.pdf",
    ):
        _touch(source / name)

    matches = AdasSourceInventory(source).matching_documents("calibration procedure for the 2026 Ford F-150 CCM")

    assert matches
    assert matches[0]["path"].name == "2026 Ford Truck F 150 4WD CCM.pdf"
    assert matches[0]["score"] >= 10
    assert matches[0]["descriptor"]["model"] == "F-150"
    assert matches[0]["descriptor"]["topic"].casefold() == "ccm"


def test_adas_procedure_headings_remove_table_of_contents_dot_leaders() -> None:
    headings = AdasSICapability._headings(
        [(1, "6.2 Lane Change Assistance, Calibrating . . . . . . . . 282")]
    )

    assert headings == [
        {"number": "6.2", "title": "Lane Change Assistance, Calibrating", "page": 1}
    ]


def test_adas_source_inventory_preserves_operator_verification_semantics(tmp_path: Path):
    source = tmp_path / "ADAS SI"
    _touch(source / "2018 Audi A5 electronics.pdf")

    result = AdasSourceInventory(source).snapshot()

    assert result["verification"]["source_library_status"] == "operator_verified"
    assert result["verification"]["verified_by"] == "Otis"
    assert result["verification"]["pipeline_metrics_are_separate"] is True
    assert result["evidence_contract"]["do_not_infer_records_from_counts"] is True


def test_coverage_observation_includes_authoritative_source_inventory_without_normalized_db(tmp_path: Path, monkeypatch):
    source = tmp_path / "ADAS SI"
    _touch(source / "2018 Audi A5 electronics.pdf")
    _touch(source / "2018 Audi A5 communication.pdf")
    _touch(source / "2021 Buick Envision ACC.pdf")
    monkeypatch.setenv("XV12_ADAS_SI_SOURCE_ROOT", str(source))

    settings = Settings(
        root=tmp_path,
        app_host="127.0.0.1",
        app_port=8120,
        model_port=8121,
        model_alias="test",
        model_context_tokens=32768,
        model_max_tokens=100,
        model_temperature=0.3,
        database_path=tmp_path / "xv12.sqlite",
        attachments_path=tmp_path / "attachments",
        auth_mode="test",
        google_client_id="",
        google_client_secret="",
        google_redirect_uri="",
        owner_google_sub="owner",
        cookie_secure=False,
        adas_database_path=tmp_path / "missing-adas.sqlite",
    )

    result = adas_coverage(settings, {})

    assert result["status"] == "verified"
    assert result["normalized_database"]["available"] is False
    source_inventory = result["authoritative_source_inventory"]
    assert source_inventory["summary"]["document_count"] == 3
    assert source_inventory["summary"]["vehicle_application_count"] == 2
    assert source_inventory["summary"]["returned_vehicle_application_count"] == 2
    assert {(item["year"], item["make"], item["model"]) for item in source_inventory["applications"]} == {
        (2018, "Audi", "A5"),
        (2021, "Buick", "Envision"),
    }
    assert "authoritative_source_inventory" in result["answering_guidance"]["all_or_unique_vehicle_requests"]
    assert result["entity_semantics"]["counts_are_not_interchangeable"] is True


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
