from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.registry import CapabilityRegistry

from .conftest import login


pytestmark = [pytest.mark.permissions, pytest.mark.admin_capabilities]


def _grant(client: TestClient, user_id: str, grants: list[dict]) -> dict:
    response = client.put(f"/api/admin/capabilities/users/{user_id}/grants", json={"grants": grants})
    assert response.status_code == 200, response.text
    return response.json()


def test_web_only_and_web_adas_combinations_apply_immediately(app) -> None:
    app.state.gateway.register("web.current.search", lambda arguments: {"status": "success", "query": arguments["query"], "sources": [{"title": "live"}]})
    with TestClient(app) as admin_client, TestClient(app) as user_one, TestClient(app) as user_two:
        login(admin_client, "admin")
        first = login(user_one, "user-a")
        second = login(user_two, "user-b")
        _grant(admin_client, first["id"], [{"family": "web", "scopes": ["read"]}])
        _grant(admin_client, second["id"], [{"family": "web", "scopes": ["read"]}, {"family": "adas_si", "scopes": ["read"]}])

        assert user_one.post("/api/capabilities/web.current.search", json={"arguments": {"query": "current"}}).status_code == 200
        assert user_one.post("/api/capabilities/adas.si.inventory.read", json={"arguments": {}}).status_code == 403
        assert user_one.post("/api/capabilities/calibration_iq.repair_orders.read", json={"arguments": {}}).status_code == 403

        assert user_two.post("/api/capabilities/web.current.search", json={"arguments": {"query": "current"}}).status_code == 200
        assert user_two.post("/api/capabilities/adas.si.inventory.read", json={"arguments": {}}).status_code == 200
        assert user_two.post("/api/capabilities/calibration_iq.repair_orders.read", json={"arguments": {}}).status_code == 403

        _grant(admin_client, first["id"], [{"family": "web", "scopes": ["read"]}, {"family": "adas_si", "scopes": ["read"]}])
        assert user_one.post("/api/capabilities/adas.si.inventory.read", json={"arguments": {}}).status_code == 200
        _grant(admin_client, first["id"], [{"family": "web", "scopes": ["read"]}])
        assert user_one.post("/api/capabilities/adas.si.inventory.read", json={"arguments": {}}).status_code == 403


def test_admin_has_implicit_access_and_grants_cannot_exceed_role_ceiling(app) -> None:
    with TestClient(app) as admin_client, TestClient(app) as user_client:
        admin = login(admin_client, "admin")
        user = login(user_client, "user-a")
        assert len(app.state.registry.list_for(admin)) == len(app.state.registry.capabilities)
        response = admin_client.put(
            f"/api/admin/capabilities/users/{user['id']}/grants",
            json={"grants": [{"family": "calibration_iq", "scopes": ["modify"]}]},
        )
        assert response.status_code == 400
        assert admin_client.post("/api/capabilities/adas.si.inventory.read", json={"arguments": {}}).status_code == 200


def test_registry_driven_admin_catalog_discovers_new_family_without_ui_change(tmp_path: Path) -> None:
    source = json.loads(Path("config/capabilities.v1.json").read_text(encoding="utf-8"))
    source["capabilities"].append({
        "id": "future.calendar.read", "family": "calendar", "family_label": "Calendar",
        "family_description": "Future dynamic test family.", "description": "Read calendar events.",
        "version": "0.0.1", "risk_tier": 0, "authorization": {"roles": ["admin", "user"]},
        "supported_scopes": ["read"], "operation_scope": "read", "classification": "read",
        "role_scope_ceiling": {"admin": ["read"], "user": ["read"]},
        "arguments_schema": {"type": "object"}, "result_schema": {"type": "object"},
        "timeout_seconds": 5, "health": "available", "execution_environment": "test", "idempotency": "safe",
    })
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    catalog = CapabilityRegistry(path).permission_catalog("user")
    assert next(item for item in catalog if item["family"] == "calendar")["allowed_scopes"] == ["read"]
    ui = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "catalog.families.forEach" in ui
    assert "future.calendar.read" not in ui
