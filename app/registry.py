from __future__ import annotations

import json
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


class CapabilityRegistry:
    def __init__(self, path: Path) -> None:
        self.document = json.loads(path.read_text(encoding="utf-8"))
        self.capabilities = {item["id"]: item for item in self.document["capabilities"]}

    @property
    def version(self) -> str:
        return str(self.document["registry_version"])

    def list_for(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        return [item for item in self.capabilities.values() if user["role"] in item["authorization"]["roles"]]

    def authorize(self, capability_id: str, user: dict[str, Any]) -> AuthorizationDecision:
        capability = self.capabilities.get(capability_id)
        if not capability:
            raise CapabilityNotFound(capability_id)
        allowed = user["role"] in capability["authorization"]["roles"]
        if user["role"] != "admin" and int(capability["risk_tier"]) >= 2:
            allowed = False
        return AuthorizationDecision(
            allowed=allowed,
            capability_id=capability_id,
            user_id=user["id"],
            role=user["role"],
            reason="role_and_risk_allowed" if allowed else "role_or_risk_denied",
        )


class CapabilityGateway:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(self, capability_id: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        if capability_id not in self.registry.capabilities:
            raise CapabilityNotFound(capability_id)
        self.handlers[capability_id] = handler

    def execute(self, capability_id: str, user: dict[str, Any], arguments: dict[str, Any]) -> tuple[Any, AuthorizationDecision]:
        decision = self.registry.authorize(capability_id, user)
        if not decision.allowed:
            raise CapabilityDenied(decision.reason)
        handler = self.handlers.get(capability_id)
        if not handler:
            raise CapabilityNotFound(f"No handler registered for {capability_id}")
        return handler(arguments), decision
