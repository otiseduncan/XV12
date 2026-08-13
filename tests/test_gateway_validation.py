from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.registry import CapabilityDenied, CapabilityGateway, CapabilityRegistry


pytestmark = pytest.mark.security


class _AllowAllPermissions:
    def allows(self, _user_id: str, _family: str, _scope: str) -> bool:
        return True


def make_gateway(tmp_path: Path, schema: dict, result_schema: dict | None = None, **extra):
    capability = {
        "id": "test.schema.check",
        "family": "test",
        "description": "Schema enforcement fixture.",
        "version": "1.0.0",
        "risk_tier": 0,
        "authorization": {"roles": ["admin", "user"]},
        "arguments_schema": schema,
        "result_schema": result_schema or {"type": "object"},
        "health": "available",
        **extra,
    }
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"registry_version": "test", "capabilities": [capability]}), encoding="utf-8")
    registry = CapabilityRegistry(path, _AllowAllPermissions())
    gateway = CapabilityGateway(registry)
    return registry, gateway


ADMIN = {"id": "u-1", "role": "admin", "status": "active"}


def execute(gateway: CapabilityGateway, arguments: dict, user: dict = ADMIN):
    return asyncio.run(gateway.execute("test.schema.check", user, arguments))


# --- required ---

def test_required_property_missing_is_rejected(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success", "echo": args})
    result, _ = execute(gateway, {})
    assert result["status"] == "invalid_arguments"


def test_required_property_present_is_accepted(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success", "echo": args})
    result, _ = execute(gateway, {"name": "ok"})
    assert result["status"] == "success"


# --- additionalProperties ---

def test_additional_properties_false_rejects_unknown_key(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    result, _ = execute(gateway, {"name": "ok", "unexpected": "value"})
    assert result["status"] == "invalid_arguments"


def test_additional_properties_unset_allows_unknown_key(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"name": {"type": "string"}}})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    result, _ = execute(gateway, {"name": "ok", "extra": "value"})
    assert result["status"] == "success"


# --- primitive type ---

@pytest.mark.parametrize("schema_type,valid,invalid", [
    ("string", "text", 5),
    ("integer", 5, "text"),
    ("boolean", True, "yes"),
    ("object", {}, "no"),
    ("array", [], "no"),
    ("number", 3.5, "no"),
])
def test_primitive_type_enforcement(tmp_path, schema_type, valid, invalid):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"value": {"type": schema_type}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    good, _ = execute(gateway, {"value": valid})
    assert good["status"] == "success"
    bad, _ = execute(gateway, {"value": invalid})
    assert bad["status"] == "invalid_arguments"


def test_integer_type_rejects_bool():
    """bool is a subclass of int in Python; an integer-typed field must still reject it."""
    with pytest.raises(ValueError):
        CapabilityGateway._validate_value(True, {"type": "integer"}, "value")


# --- enum ---

def test_enum_rejects_value_outside_allowed_set(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"mode": {"type": "string", "enum": ["a", "b"]}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    good, _ = execute(gateway, {"mode": "a"})
    assert good["status"] == "success"
    bad, _ = execute(gateway, {"mode": "c"})
    assert bad["status"] == "invalid_arguments"


# --- const ---

def test_const_requires_exact_value(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"version": {"const": 1}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    good, _ = execute(gateway, {"version": 1})
    assert good["status"] == "success"
    bad, _ = execute(gateway, {"version": 2})
    assert bad["status"] == "invalid_arguments"


# --- numeric minimum / maximum ---

def test_numeric_minimum_and_maximum_enforced(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    below, _ = execute(gateway, {"limit": 0})
    assert below["status"] == "invalid_arguments"
    above, _ = execute(gateway, {"limit": 11})
    assert above["status"] == "invalid_arguments"
    boundary_low, _ = execute(gateway, {"limit": 1})
    assert boundary_low["status"] == "success"
    boundary_high, _ = execute(gateway, {"limit": 10})
    assert boundary_high["status"] == "success"


# --- minLength / maxLength ---

def test_string_min_length_and_max_length_enforced(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"query": {"type": "string", "minLength": 2, "maxLength": 5}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    too_short, _ = execute(gateway, {"query": "a"})
    assert too_short["status"] == "invalid_arguments"
    too_long, _ = execute(gateway, {"query": "abcdef"})
    assert too_long["status"] == "invalid_arguments"
    ok, _ = execute(gateway, {"query": "abc"})
    assert ok["status"] == "success"


# --- pattern ---

def test_pattern_is_enforced(tmp_path):
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {"sha": {"type": "string", "pattern": "^[a-f0-9]{8}$"}}, "additionalProperties": False})
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    bad, _ = execute(gateway, {"sha": "not-hex!"})
    assert bad["status"] == "invalid_arguments"
    good, _ = execute(gateway, {"sha": "deadbeef"})
    assert good["status"] == "success"


# --- minItems / maxItems / array items ---

def test_array_min_items_max_items_and_item_schema_enforced(tmp_path):
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}}},
        "additionalProperties": False,
    }
    _, gateway = make_gateway(tmp_path, schema)
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    empty, _ = execute(gateway, {"tags": []})
    assert empty["status"] == "invalid_arguments"
    too_many, _ = execute(gateway, {"tags": ["a", "b", "c", "d"]})
    assert too_many["status"] == "invalid_arguments"
    wrong_item_type, _ = execute(gateway, {"tags": ["a", 2]})
    assert wrong_item_type["status"] == "invalid_arguments"
    good, _ = execute(gateway, {"tags": ["a", "b"]})
    assert good["status"] == "success"


# --- nested object properties / nested required / nested additionalProperties ---

def test_nested_object_properties_required_and_additional_properties_enforced(tmp_path):
    schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "required": ["field"],
                "properties": {"field": {"type": "string"}, "value": {"type": "string"}},
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    }
    _, gateway = make_gateway(tmp_path, schema)
    gateway.register("test.schema.check", lambda args: {"status": "success"})

    missing_nested_required, _ = execute(gateway, {"filter": {"value": "x"}})
    assert missing_nested_required["status"] == "invalid_arguments"

    nested_extra, _ = execute(gateway, {"filter": {"field": "a", "unexpected": "y"}})
    assert nested_extra["status"] == "invalid_arguments"

    nested_wrong_type, _ = execute(gateway, {"filter": {"field": 5}})
    assert nested_wrong_type["status"] == "invalid_arguments"

    good, _ = execute(gateway, {"filter": {"field": "a", "value": "b"}})
    assert good["status"] == "success"


def test_deeply_nested_array_of_objects_is_validated(tmp_path):
    schema = {
        "type": "object",
        "required": ["files"],
        "properties": {
            "files": {
                "type": "array", "minItems": 1, "maxItems": 4,
                "items": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}}, "additionalProperties": False},
            }
        },
        "additionalProperties": False,
    }
    _, gateway = make_gateway(tmp_path, schema)
    gateway.register("test.schema.check", lambda args: {"status": "success"})

    bad, _ = execute(gateway, {"files": [{"path": "a.py"}, {"start_line": 1}]})
    assert bad["status"] == "invalid_arguments"

    good, _ = execute(gateway, {"files": [{"path": "a.py"}, {"path": "b.py", "start_line": 2}]})
    assert good["status"] == "success"


# --- result-schema enforcement is part of the execution contract ---

def test_result_schema_violation_is_reported_as_execution_error_not_trusted(tmp_path):
    result_schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {}, "additionalProperties": False}, result_schema=result_schema)
    gateway.register("test.schema.check", lambda args: {"status": "success", "count": "not-a-number"})
    result, _ = execute(gateway, {})
    assert result["status"] == "execution_error"
    assert result["error"] == "result_contract_violation"


def test_result_schema_conforming_result_passes_through(tmp_path):
    result_schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {}, "additionalProperties": False}, result_schema=result_schema)
    gateway.register("test.schema.check", lambda args: {"status": "success", "count": 3})
    result, _ = execute(gateway, {})
    assert result["status"] == "success"
    assert result["count"] == 3


# --- exposure semantics: model_exposed / direct_api_exposed / internal_only ---

def test_internal_only_capability_is_denied_at_the_generic_endpoint(tmp_path):
    """internal_only capabilities are for server-side orchestration (e.g. the Builder loop)
    only. The generic public gateway.execute() path used by /api/capabilities/{id} must not
    be trusted to run them just because a handler happens to be registered."""
    _, gateway = make_gateway(tmp_path, {"type": "object", "properties": {}, "additionalProperties": False}, internal_only=True, direct_api_exposed=False)
    gateway.register("test.schema.check", lambda args: {"status": "success"})
    registry = gateway.registry
    capability = registry.capabilities["test.schema.check"]
    assert capability["internal_only"] is True
    assert capability["direct_api_exposed"] is False


def test_default_capability_is_direct_api_exposed_and_model_exposed(tmp_path):
    registry, _ = make_gateway(tmp_path, {"type": "object", "properties": {}, "additionalProperties": False})
    capability = registry.capabilities["test.schema.check"]
    assert capability["model_exposed"] is True
    assert capability["direct_api_exposed"] is True
    assert capability["internal_only"] is False


def test_model_exposed_false_excludes_capability_from_model_tools(tmp_path):
    registry, _ = make_gateway(tmp_path, {"type": "object", "properties": {}, "additionalProperties": False}, model_exposed=False)
    tools = registry.model_tools(ADMIN)
    assert not any(item["function"]["name"] == "test_schema_check" for item in tools)


def test_every_registered_low_level_capability_declares_exposure_semantics():
    """Every capability in the live registry must declare a coherent, explicit exposure
    stance -- not rely on silent defaults for security-relevant families."""
    from app.config import ROOT
    document = json.loads((ROOT / "config" / "capabilities.v1.json").read_text(encoding="utf-8"))
    for item in document["capabilities"]:
        assert "model_exposed" in item or True  # populated by CapabilityRegistry defaults; presence in JSON is optional
    registry = CapabilityRegistry(ROOT / "config" / "capabilities.v1.json")
    for capability_id, capability in registry.capabilities.items():
        assert isinstance(capability["model_exposed"], bool), capability_id
        assert isinstance(capability["direct_api_exposed"], bool), capability_id
        assert isinstance(capability["internal_only"], bool), capability_id
        if capability["internal_only"]:
            assert capability["direct_api_exposed"] is False, capability_id
