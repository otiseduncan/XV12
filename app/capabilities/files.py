from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class LocalFilesCapability:
    """Structured filesystem access with bounded roots and managed-only mutations."""

    def __init__(self, read_roots: list[Path], managed_root: Path) -> None:
        self.read_roots = [path.resolve() for path in read_roots]
        self.managed_root = managed_root.resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _within(path: Path, roots: list[Path]) -> bool:
        return any(path == root or path.is_relative_to(root) for root in roots)

    def _read_path(self, raw: str) -> Path:
        path = Path(raw).resolve()
        if not self._within(path, self.read_roots):
            raise ValueError("Path is outside configured read roots.")
        return path

    def _managed_path(self, raw: str, user: dict[str, Any]) -> Path:
        user_root = (self.managed_root / user["id"]).resolve()
        user_root.mkdir(parents=True, exist_ok=True)
        candidate = Path(raw)
        path = (user_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if path == user_root or not path.is_relative_to(user_root):
            raise ValueError("Writes are restricted to the authenticated user's managed root.")
        return path

    @staticmethod
    def _metadata(path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "modified_ns": path.stat().st_mtime_ns}

    def read(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        path = self._read_path(str(arguments.get("path") or ""))
        if not path.exists():
            return {"status": "no_result", "path": str(path)}
        if path.is_dir():
            limit = min(max(int(arguments.get("limit") or 100), 1), 250)
            items = []
            for item in sorted(path.iterdir(), key=lambda value: value.name.casefold())[:limit]:
                items.append({"name": item.name, "path": str(item), "kind": "directory" if item.is_dir() else "file", "bytes": item.stat().st_size if item.is_file() else None})
            return {"status": "success", "path": str(path), "items": items, "truncated": len(items) == limit}
        maximum = min(max(int(arguments.get("max_bytes") or 131072), 1), 524288)
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            return {"status": "partial_success", **self._metadata(path), "message": "Binary file metadata returned; content was not decoded."}
        return {"status": "success", **self._metadata(path), "content": data[:maximum].decode("utf-8", errors="replace"), "truncated": len(data) > maximum}

    def write(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        path = self._managed_path(str(arguments.get("path") or ""), user)
        if path.exists():
            raise ValueError("Managed file already exists; use modify with its expected SHA256.")
        content = str(arguments.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return {"status": "success", "operation": "write", "receipt": {**self._metadata(path), "user_id": user["id"]}}

    def modify(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        path = self._managed_path(str(arguments.get("path") or ""), user)
        if not path.is_file():
            return {"status": "no_result", "path": str(path)}
        before = self._metadata(path)
        expected = str(arguments.get("expected_sha256") or "")
        if before["sha256"] != expected:
            raise ValueError("File changed before modification; expected SHA256 does not match.")
        path.write_text(str(arguments.get("content") or ""), encoding="utf-8", newline="\n")
        return {"status": "success", "operation": "modify", "receipt": {"before_sha256": before["sha256"], **self._metadata(path), "user_id": user["id"]}}
