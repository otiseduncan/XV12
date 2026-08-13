from __future__ import annotations

import asyncio
import json
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class CapabilityNotFound(KeyError):
    pass


class CapabilityDenied(PermissionError):
    pass


TRUTH_CONTRACT = (
    "Authoritative records in the final answer must come from records actually returned by this capability. "
    "Counts and summaries do not authorize inventing or inferring missing itemized rows. "
    "If the user asks to list underlying records and this result does not enumerate them, use an available listing/inventory capability or state that the records were not returned."
)


@dataclass(slots=True)
class AuthorizationDecision:
    allowed: bool
    capability_id: str
    user_id: str
    role: str
    reason: str
    family: str = ""
    scope: str = ""


class CapabilityRegistry:
    def __init__(self, path: Path, permissions: Any | None = None) -> None:
        self.document = json.loads(path.read_text(encoding="utf-8"))
        failure_semantics = ["success", "partial_success", "no_result", "unavailable", "timeout", "invalid_arguments", "permission_denied", "approval_required", "cancelled", "execution_error"]
        ids: set[str] = set()
        for item in self.document["capabilities"]:
            capability_id = str(item.get("id") or "").strip()
            if not capability_id:
                raise ValueError("Every capability must declare a non-empty id.")
            if capability_id in ids:
                raise ValueError(f"Duplicate capability id: {capability_id}")
            ids.add(capability_id)
            if not item.get("family"):
                raise ValueError(f"Capability {capability_id} must declare a family.")
            if not isinstance(item.get("arguments_schema"), dict) or item["arguments_schema"].get("type") != "object":
                raise ValueError(f"Capability {capability_id} must declare an object arguments_schema.")
            item.setdefault("supported_scopes", [item.get("operation_scope") or "read"])
            item.setdefault("operation_scope", item["supported_scopes"][0])
            item.setdefault("classification", item["operation_scope"])
            item.setdefault("role_scope_ceiling", {role: list(item["supported_scopes"]) for role in item["authorization"]["roles"]})
            item.setdefault("failure_semantics", failure_semantics)
            item.setdefault("result_schema", {"type": "object"})
            # Exposure semantics are three distinct, explicit concepts:
            #   model_exposed      — the ordinary conversational model receives the function;
            #   direct_api_exposed — the generic /api/capabilities endpoint may execute it;
            #   internal_only      — only server-side orchestration (e.g. the Builder loop)
            #                        may execute it; the public endpoint must refuse.
            item.setdefault("model_exposed", True)
            item.setdefault("internal_only", False)
            item.setdefault("direct_api_exposed", not item["internal_only"])
        self.capabilities = {item["id"]: item for item in self.document["capabilities"]}
        self.permissions = permissions

    @property
    def version(self) -> str:
        return str(self.document["registry_version"])

    def list_for(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in self.capabilities.values() if self.authorize(item["id"], user).allowed]

    @staticmethod
    def _supported_scopes(item: dict[str, Any]) -> list[str]:
        return list(item.get("supported_scopes") or [item.get("operation_scope") or "read"])

    @staticmethod
    def _role_ceiling(item: dict[str, Any], role: str) -> list[str]:
        ceilings = item.get("role_scope_ceiling") or {}
        if role in ceilings:
            return list(ceilings[role])
        return CapabilityRegistry._supported_scopes(item) if role in item["authorization"]["roles"] else []

    def permission_catalog(self, role: str = "user") -> list[dict[str, Any]]:
        families: dict[str, dict[str, Any]] = {}
        for item in self.capabilities.values():
            if item.get("platform_service"):
                continue
            allowed = self._role_ceiling(item, role)
            if not allowed:
                continue
            family = str(item["family"])
            record = families.setdefault(
                family,
                {
                    "family": family,
                    "label": item.get("family_label") or family.replace("_", " ").replace("-", " ").title(),
                    "description": item.get("family_description") or item["description"],
                    "allowed_scopes": [],
                    "capabilities": [],
                    "health": "available",
                },
            )
            record["allowed_scopes"] = sorted(set(record["allowed_scopes"]) | set(allowed))
            record["capabilities"].append(item["id"])
            if item.get("health") not in {"available", "dynamic"}:
                record["health"] = item.get("health", "unavailable")
        return sorted(families.values(), key=lambda item: item["label"].casefold())

    @staticmethod
    def tool_name(capability_id: str) -> str:
        return capability_id.replace(".", "_")

    def capability_id_for_tool(self, tool_name: str) -> str:
        for capability_id in self.capabilities:
            if self.tool_name(capability_id) == tool_name:
                return capability_id
        raise CapabilityNotFound(tool_name)

    def model_tools(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        items = self.list_for(user)
        model_tool_filter = getattr(self, "model_tool_filter", None)
        if callable(model_tool_filter):
            items = model_tool_filter(items, user)
        return [
            {
                "type": "function",
                "function": {
                    "name": self.tool_name(item["id"]),
                    "description": f"Registry family: {item['family']}. Registry health: {item['health']}. {item['description']} Evidence rule: {TRUTH_CONTRACT}",
                    "parameters": item["arguments_schema"],
                },
            }
            for item in items
            if item.get("model_exposed", True) and item["id"] in self.capabilities
        ]

    def authorize(self, capability_id: str, user: dict[str, Any]) -> AuthorizationDecision:
        capability = self.capabilities.get(capability_id)
        if not capability:
            raise CapabilityNotFound(capability_id)
        family = str(capability["family"])
        scope = str(capability.get("operation_scope") or "read")
        ceiling = self._role_ceiling(capability, user["role"])
        allowed = user.get("status", "active") == "active" and scope in ceiling
        reason = "role_scope_allowed" if allowed else "role_scope_denied"
        if allowed and capability.get("platform_service"):
            reason = "authenticated_platform_service"
            return AuthorizationDecision(True, capability_id, user["id"], user["role"], reason, family, scope)
        if allowed and user["role"] != "admin" and int(capability["risk_tier"]) >= 2:
            allowed, reason = False, "risk_tier_denied"
        if allowed and user["role"] != "admin" and self.permissions is not None:
            allowed = self.permissions.allows(user["id"], family, scope)
            reason = "user_grant_allowed" if allowed else "capability_grant_missing"
        if allowed and user["role"] == "admin":
            reason = "administrator_implicit_access"
        return AuthorizationDecision(
            allowed=allowed,
            capability_id=capability_id,
            user_id=user["id"],
            role=user["role"],
            reason=reason,
            family=family,
            scope=scope,
        )


class CapabilityGateway:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.audit_logger: Any | None = None

    def register(self, capability_id: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        if capability_id not in self.registry.capabilities:
            raise CapabilityNotFound(capability_id)
        self.handlers[capability_id] = handler

    _SCHEMA_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "number": (int, float), "null": type(None)}
    _SCHEMA_MAX_DEPTH = 12

    @classmethod
    def _validate_value(cls, value: Any, spec: dict[str, Any], label: str, depth: int = 0) -> None:
        """Complete bounded validator for the JSON Schema subset the XV12 registry declares.
        The registry is an execution contract: every constraint a capability declares is
        enforced here, recursively, before its handler runs."""
        if depth > cls._SCHEMA_MAX_DEPTH:
            raise ValueError(f"{label} exceeds the supported schema nesting depth.")
        if not isinstance(spec, dict):
            return
        declared = spec.get("type")
        if declared:
            expected = cls._SCHEMA_TYPES.get(declared)
            if expected is None:
                raise ValueError(f"{label} declares unsupported schema type {declared!r}.")
            if not isinstance(value, expected) or (declared in {"integer", "number"} and isinstance(value, bool)):
                raise ValueError(f"{label} must be {declared}.")
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"{label} is not an allowed value.")
        if "const" in spec and value != spec["const"]:
            raise ValueError(f"{label} must equal {spec['const']!r}.")
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            if "minimum" in spec and value < spec["minimum"]:
                raise ValueError(f"{label} is below its minimum of {spec['minimum']}.")
            if "maximum" in spec and value > spec["maximum"]:
                raise ValueError(f"{label} exceeds its maximum of {spec['maximum']}.")
        if isinstance(value, str):
            if "minLength" in spec and len(value) < int(spec["minLength"]):
                raise ValueError(f"{label} is shorter than its minLength of {spec['minLength']}.")
            if "maxLength" in spec and len(value) > int(spec["maxLength"]):
                raise ValueError(f"{label} exceeds its maxLength of {spec['maxLength']}.")
            if "pattern" in spec and not re.search(str(spec["pattern"]), value):
                raise ValueError(f"{label} does not match its required pattern.")
        if isinstance(value, list):
            if "minItems" in spec and len(value) < int(spec["minItems"]):
                raise ValueError(f"{label} has fewer than its minItems of {spec['minItems']}.")
            if "maxItems" in spec and len(value) > int(spec["maxItems"]):
                raise ValueError(f"{label} exceeds its maxItems of {spec['maxItems']}.")
            items_spec = spec.get("items")
            if isinstance(items_spec, dict):
                for index, item in enumerate(value):
                    cls._validate_value(item, items_spec, f"{label}[{index}]", depth + 1)
        if isinstance(value, dict):
            properties = spec.get("properties") or {}
            missing = sorted(set(spec.get("required") or []) - set(value))
            if missing:
                raise ValueError(f"{label} is missing required properties: {', '.join(missing)}.")
            if spec.get("additionalProperties") is False:
                extra = sorted(set(value) - set(properties))
                if extra:
                    raise ValueError(f"{label} has unexpected properties: {', '.join(extra)}.")
            for name, item in value.items():
                if name in properties:
                    cls._validate_value(item, properties[name], f"{label}.{name}", depth + 1)

    @classmethod
    def _validate(cls, arguments: dict[str, Any], schema: dict[str, Any]) -> None:
        cls._validate_value(arguments, schema, "arguments")

    @staticmethod
    def _attach_evidence_contract(result: dict[str, Any], capability_id: str) -> dict[str, Any]:
        contract = result.get("evidence_contract")
        if not isinstance(contract, dict):
            contract = {}
        result["evidence_contract"] = {
            "capability_id": capability_id,
            "authoritative_records_only": True,
            "specific_records_must_be_present_in_result": True,
            "counts_do_not_imply_missing_rows": True,
            **contract,
        }
        return result

    async def execute(self, capability_id: str, user: dict[str, Any], arguments: dict[str, Any]) -> tuple[Any, AuthorizationDecision]:
        decision = self.registry.authorize(capability_id, user)
        if not decision.allowed:
            if self.audit_logger:
                self.audit_logger.info(json.dumps({"event": "security.privileged_action_denied", "user_id": user["id"], "capability_id": capability_id, "reason": decision.reason}))
            raise CapabilityDenied(decision.reason)
        handler = self.handlers.get(capability_id)
        if not handler:
            raise CapabilityNotFound(f"No handler registered for {capability_id}")
        try:
            capability = self.registry.capabilities[capability_id]
            self._validate(arguments, capability["arguments_schema"])
            timeout = float(capability.get("timeout_seconds") or 30)
            if inspect.iscoroutinefunction(handler):
                pending = handler(arguments, user) if len(inspect.signature(handler).parameters) >= 2 else handler(arguments)
            else:
                pending = asyncio.to_thread(handler, arguments, user) if len(inspect.signature(handler).parameters) >= 2 else asyncio.to_thread(handler, arguments)
            result = await asyncio.wait_for(pending, timeout=timeout)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            if not isinstance(result, dict):
                result = {"status": "success", "result": result}
            elif "status" not in result:
                result = {"status": "success", **result}
            elif result["status"] not in {"success", "partial_success", "no_result", "unavailable", "timeout", "invalid_arguments", "permission_denied", "approval_required", "cancelled", "execution_error"}:
                domain_status = str(result["status"])
                mapped = {
                    "offline": "unavailable", "not_configured": "unavailable", "authentication_failed": "permission_denied",
                    "invalid_request": "invalid_arguments", "failed": "execution_error",
                }.get(domain_status, "success")
                result = {**result, "status": mapped, "domain_status": domain_status}
            # The declared result_schema is part of the execution contract: a handler that
            # returns a shape violating its own declaration is an execution error, reported
            # safely rather than passed to the model as trustworthy evidence.
            try:
                self._validate_value(result, capability.get("result_schema") or {}, "result")
            except ValueError as contract_error:
                return {
                    "status": "execution_error",
                    "error": "result_contract_violation",
                    "message": str(contract_error)[:500],
                }, decision
            if result["status"] in {"success", "partial_success", "no_result"}:
                result = self._attach_evidence_contract(result, capability_id)
            if self.audit_logger and user.get("role") == "admin":
                self.audit_logger.info(json.dumps({"event": "security.owner_capability_executed", "user_id": user["id"], "capability_id": capability_id, "status": result.get("status")}))
            return result, decision
        except (TimeoutError, asyncio.TimeoutError):
            return {"status": "timeout", "message": "Capability execution timed out."}, decision
        except (TypeError, ValueError) as error:
            return {"status": "invalid_arguments", "message": str(error)[:500]}, decision
        except Exception as error:
            return {"status": "execution_error", "error": type(error).__name__, "message": "Capability execution failed safely."}, decision
