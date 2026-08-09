from __future__ import annotations

import asyncio
import json
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class CapabilityNotFound(KeyError):
    pass


class CapabilityDenied(PermissionError):
    pass


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
        for item in self.document["capabilities"]:
            item.setdefault("supported_scopes", [item.get("operation_scope") or "read"])
            item.setdefault("operation_scope", item["supported_scopes"][0])
            item.setdefault("classification", item["operation_scope"])
            item.setdefault("role_scope_ceiling", {role: list(item["supported_scopes"]) for role in item["authorization"]["roles"]})
            item.setdefault("failure_semantics", failure_semantics)
            item.setdefault("result_schema", {"type": "object"})
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
        return [
            {
                "type": "function",
                "function": {
                    "name": self.tool_name(item["id"]),
                    "description": f"Registry family: {item['family']}. Registry health: {item['health']}. {item['description']}",
                    "parameters": item["arguments_schema"],
                },
            }
            for item in self.list_for(user)
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

    def register(self, capability_id: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        if capability_id not in self.registry.capabilities:
            raise CapabilityNotFound(capability_id)
        self.handlers[capability_id] = handler

    @staticmethod
    def _validate(arguments: dict[str, Any], schema: dict[str, Any]) -> None:
        missing = sorted(set(schema.get("required") or []) - set(arguments))
        if missing:
            raise ValueError(f"Missing required arguments: {', '.join(missing)}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = sorted(set(arguments) - set(properties))
            if extra:
                raise ValueError(f"Unexpected arguments: {', '.join(extra)}")
        types = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list, "number": (int, float)}
        for name, value in arguments.items():
            spec = properties.get(name) or {}
            expected = types.get(spec.get("type"))
            if expected and (not isinstance(value, expected) or spec.get("type") == "integer" and isinstance(value, bool)):
                raise ValueError(f"Argument {name} must be {spec['type']}.")
            if "enum" in spec and value not in spec["enum"]:
                raise ValueError(f"Argument {name} is not an allowed value.")
            if "const" in spec and value != spec["const"]:
                raise ValueError(f"Argument {name} must equal {spec['const']}.")
            if isinstance(value, (int, float)) and "minimum" in spec and value < spec["minimum"]:
                raise ValueError(f"Argument {name} is below its minimum.")
            if isinstance(value, (int, float)) and "maximum" in spec and value > spec["maximum"]:
                raise ValueError(f"Argument {name} exceeds its maximum.")

    async def execute(self, capability_id: str, user: dict[str, Any], arguments: dict[str, Any]) -> tuple[Any, AuthorizationDecision]:
        decision = self.registry.authorize(capability_id, user)
        if not decision.allowed:
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
            return result, decision
        except (TimeoutError, asyncio.TimeoutError):
            return {"status": "timeout", "message": "Capability execution timed out."}, decision
        except (TypeError, ValueError) as error:
            return {"status": "invalid_arguments", "message": str(error)[:500]}, decision
        except Exception as error:
            return {"status": "execution_error", "error": type(error).__name__, "message": "Capability execution failed safely."}, decision
