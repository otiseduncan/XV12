from __future__ import annotations

import uuid
from typing import Any

import httpx

from ..config import Settings
from ..data_tools import _calibration_env, calibration_iq_read


class CalibrationIQCapability:
    """Cohesive adapter to Calibration IQ's versioned tool API."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def read(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        result = await calibration_iq_read(self.settings, arguments)
        items = list(result.get("items") or [])
        if items:
            rows = [
                {
                    "RO": item.get("ro_number"),
                    "Vehicle": " ".join(str(value) for value in (item.get("year"), item.get("make"), item.get("model")) if value),
                    "Status": item.get("display_status") or item.get("status"),
                    "Shop": (item.get("shop") or {}).get("name") if isinstance(item.get("shop"), dict) else item.get("shop"),
                }
                for item in items[:20]
            ]
            result["artifacts"] = [{
                "id": f"ciq-{uuid.uuid5(uuid.NAMESPACE_URL, str(result.get('count')) + json_key(rows))}",
                "type": "structured_data", "title": "Calibration IQ repair orders",
                "mime_type": "application/json", "source": "Calibration IQ", "reference": None,
                "preview": {"kind": "table"}, "downloadable": False, "printable": False,
                "copyable": True, "metadata": {"count": result.get("count"), "returned": len(rows)}, "data": rows,
            }]
        return result

    async def mutate(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        token = _calibration_env(self.settings.calibration_iq_project_path).get("TOOL_SERVICE_TOKEN", "")
        if not token:
            return {"status": "unavailable", "message": "Calibration IQ service credentials are not configured."}
        ro_id = str(arguments.get("repair_order_id") or "")
        operation = str(arguments.get("operation") or "")
        if operation not in {"change_status", "update_ro", "update_blocker", "update_requirement"}:
            raise ValueError("Unsupported Calibration IQ mutation operation.")
        key = str(arguments.get("idempotency_key") or "")
        if len(key) < 16:
            raise ValueError("idempotency_key must be at least 16 characters")
        correlation = str(arguments.get("correlation_id") or f"xv12-{uuid.uuid4().hex[:16]}")
        body = {"operation": operation, "arguments": dict(arguments.get("arguments") or {}), "expected_version": int(arguments.get("expected_version") or 0), "correlation_id": correlation}
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(f"{self.settings.calibration_iq_base_url}/ros/{ro_id}/mutations", json=body, headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key})
        if response.status_code == 403:
            return {"status": "permission_denied", "upstream_status": 403}
        if response.status_code == 409:
            return {"status": "execution_error", "conflict": True, "detail": response.json().get("detail")}
        response.raise_for_status()
        result = response.json()
        receipt = dict(result.get("receipt") or {})
        receipt.update({"authenticated_user": user["id"], "target": ro_id, "requested_change": body, "idempotency_key": key, "verified": bool(result.get("success") and receipt.get("status") == "completed")})
        status = "success" if receipt["verified"] else "partial_success"
        return {
            "status": status, "duplicate": bool(result.get("duplicate")), "receipt": receipt,
            "artifacts": [{
                "id": str(receipt.get("mutation_id") or key), "type": "receipt", "title": "Calibration IQ execution receipt",
                "mime_type": "application/json", "source": "Calibration IQ", "reference": None,
                "preview": {"kind": "receipt"}, "downloadable": False, "printable": False, "copyable": True,
                "metadata": {"operation": operation, "target": ro_id, "status": receipt.get("status"), "verified": receipt.get("verified"), "duplicate": bool(result.get("duplicate"))},
                "data": {"mutation_id": receipt.get("mutation_id"), "correlation_id": receipt.get("correlation_id"), "operation": operation, "status": receipt.get("status"), "verified": receipt.get("verified")},
            }],
        }

    async def write(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if str(arguments.get("operation") or "") != "update_ro":
            raise ValueError("Write supports the safe update_ro operation only.")
        return await self.mutate(arguments, user)

    async def modify(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        return await self.mutate(arguments, user)


def json_key(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, default=str)
