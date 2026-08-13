from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
from typing import Any


# Central deterministic secret/private path policy for model-directed filesystem access.
# This guards the capability surface only: internal application code that loads its own
# configuration (app.config reading config/.env.local) never passes through this module.
SENSITIVE_NAME_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.ppk", "*.pfx", "*.p12",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    "credentials*", "*.credentials", "service-account*.json",
    "token*", "*.token", "*secret*", "*password*", "*.keystore", "*.jks",
    "*.db", "*.sqlite", "*.sqlite3",
)
SENSITIVE_DIR_NAMES = {".ssh", ".gnupg", "secrets"}

RANGED_READ_MAX_BYTES = 4_000_000
RANGED_READ_MAX_LINES = 400
BATCH_READ_MAX_FILES = 8
BATCH_READ_FILE_CHAR_LIMIT = 6000
BATCH_READ_TOTAL_CHAR_LIMIT = 24_000

# XODUZ's owner-authorized local workspace. This is intentionally outside the XV12 source
# repository: normal conversational file writes for the authenticated Owner belong here,
# while repository mutation continues to require the dedicated engineering/Builder boundary.
# The environment override exists for recovery/testing without changing the architectural
# default expected on the operator machine.
DEFAULT_XODUZ_SANDBOX_PATH = Path(os.getenv("XV12_XODUZ_SANDBOX_PATH", r"X:\xoduz-sandbox"))


class SensitivePathError(PermissionError):
    """Raised when a capability-surface read targets a secret or credential path."""


def is_sensitive_path(path: Path) -> bool:
    name = path.name.casefold()
    if any(part.casefold() in SENSITIVE_DIR_NAMES for part in path.parts):
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAME_PATTERNS)


def guard_sensitive_path(path: Path) -> None:
    if is_sensitive_path(path):
        raise SensitivePathError(
            "This path matches the protected secret/credential policy and cannot be read through file capabilities."
        )


def read_text_range(path: Path, start_line: int, end_line: int) -> dict[str, Any]:
    """Deterministic bounded ranged read with line numbers and continuation metadata.
    Shared by the user-facing Local Files capability and the admin engineering surface."""
    data = path.read_bytes()
    if len(data) > RANGED_READ_MAX_BYTES:
        data = data[:RANGED_READ_MAX_BYTES]
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = min(total, end_line if end_line >= start else start + RANGED_READ_MAX_LINES - 1)
    end = min(end, start + RANGED_READ_MAX_LINES - 1)
    window = lines[start - 1:end]
    numbered = "\n".join(f"{start + offset}: {line}" for offset, line in enumerate(window))
    return {
        "content": numbered,
        "start_line": start,
        "end_line": start + len(window) - 1 if window else start,
        "total_lines": total,
        "has_more": end < total,
        "next_start_line": end + 1 if end < total else None,
    }


class LocalFilesCapability:
    """Structured filesystem access with bounded roots and managed-only mutations.

    Read authorization is enforced deterministically per authenticated user:
    - admin retains the explicitly configured repository-wide inspection roots and XODUZ's
      owner-authorized local sandbox;
    - normal users may read only their own managed files and their own attachments.

    Mutation authorization is intentionally narrower than admin read authorization:
    - the authenticated Owner's relative/absolute Local Files writes resolve only inside
      ``X:\\xoduz-sandbox`` (or the explicit XV12_XODUZ_SANDBOX_PATH override);
    - normal-user writes remain isolated under their per-user managed root.

    This means X can create and modify local working files without gaining generic write
    authority over ``X:\\XV12`` or the rest of the host filesystem. Secret/credential paths
    remain denied for every role at this capability surface.
    """

    def __init__(
        self,
        read_roots: list[Path],
        managed_root: Path,
        artifacts: Any | None = None,
        attachments_root: Path | None = None,
        admin_sandbox_root: Path | None = None,
    ) -> None:
        self.read_roots = [path.resolve() for path in read_roots]
        self.managed_root = managed_root.resolve()
        self.artifacts = artifacts
        self.attachments_root = attachments_root.resolve() if attachments_root else None
        self.admin_sandbox_root = (admin_sandbox_root or DEFAULT_XODUZ_SANDBOX_PATH).resolve()
        self.managed_root.mkdir(parents=True, exist_ok=True)

        # Sandbox files use the existing user-scoped Artifact Store so they can still be
        # displayed/downloaded in chat. Adding this one explicit root does not make arbitrary
        # host paths artifact-eligible, and ArtifactStore ownership/authorization still applies.
        if self.artifacts is not None and hasattr(self.artifacts, "allowed_roots"):
            roots = self.artifacts.allowed_roots
            if self.admin_sandbox_root not in roots:
                roots.append(self.admin_sandbox_root)

    @staticmethod
    def _within(path: Path, roots: list[Path]) -> bool:
        return any(path == root or path.is_relative_to(root) for root in roots)

    @staticmethod
    def _is_authenticated_owner(user: dict[str, Any]) -> bool:
        # Real application identities always carry google_sub. Keeping this distinction lets
        # isolated unit fixtures that use a bare {role: admin} remain hermetic while the
        # actual authenticated Owner receives the permanent XODUZ sandbox contract.
        return user.get("role") == "admin" and bool(user.get("google_sub"))

    def _mutation_root(self, user: dict[str, Any]) -> Path:
        if self._is_authenticated_owner(user):
            root = self.admin_sandbox_root
        else:
            root = self.managed_root / str(user.get("id") or "")
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _authorized_read_roots(self, user: dict[str, Any]) -> list[Path]:
        if user.get("role") == "admin":
            return [*self.read_roots, self.admin_sandbox_root]
        roots = [self.managed_root / str(user.get("id") or "")]
        if self.attachments_root:
            roots.append(self.attachments_root / str(user.get("id") or ""))
        return [root.resolve() for root in roots]

    def _read_path(self, raw: str, user: dict[str, Any]) -> Path:
        path = Path(raw).resolve()
        if not self._within(path, self._authorized_read_roots(user)):
            raise ValueError("Path is outside the read roots authorized for this user.")
        guard_sensitive_path(path)
        return path

    def _managed_path(self, raw: str, user: dict[str, Any]) -> Path:
        mutation_root = self._mutation_root(user)
        candidate = Path(raw)
        path = (mutation_root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if path == mutation_root or not path.is_relative_to(mutation_root):
            if self._is_authenticated_owner(user):
                raise ValueError("Writes are restricted to XODUZ's local sandbox.")
            raise ValueError("Writes are restricted to the authenticated user's managed root.")
        return path

    @staticmethod
    def _metadata(path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "modified_ns": path.stat().st_mtime_ns}

    def _artifact(self, path: Path, user: dict[str, Any], relevant_text: str = "", arguments: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self.artifacts is None:
            return []
        arguments = arguments or {}
        page_start = int(arguments["page_start"]) if arguments.get("page_start") else None
        page_end = int(arguments.get("page_end") or page_start) if page_start else None
        scoped_title = path.name if not page_start else f"{path.stem} — {'Page' if page_start == page_end else 'Pages'} {page_start if page_start == page_end else f'{page_start}–{page_end}'}"
        try:
            return [self.artifacts.register_file(
                user_id=user["id"], capability_id="files.local.read", source_path=path,
                title=scoped_title, source_title=path.name, source_label="Local Files",
                requested_scope=str(arguments.get("requested_scope") or ""),
                scope_kind="page" if page_start == page_end and page_start else "section" if page_start else "full",
                page_start=page_start, page_end=page_end, section_title=str(arguments.get("section") or "") or None,
                relevant_text=relevant_text,
            )]
        except ValueError:
            return []

    def read(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        try:
            path = self._read_path(str(arguments.get("path") or ""), user)
        except (SensitivePathError, ValueError) as error:
            return {"status": "permission_denied", "message": str(error)}
        if not path.exists():
            return {"status": "no_result", "path": str(path)}
        if path.is_dir():
            limit = min(max(int(arguments.get("limit") or 100), 1), 250)
            items = []
            for item in sorted(path.iterdir(), key=lambda value: value.name.casefold())[: limit * 2]:
                if is_sensitive_path(item):
                    continue
                items.append({"name": item.name, "path": str(item), "kind": "directory" if item.is_dir() else "file", "bytes": item.stat().st_size if item.is_file() else None})
                if len(items) >= limit:
                    break
            return {"status": "success", "path": str(path), "items": items, "truncated": len(items) == limit}
        maximum = min(max(int(arguments.get("max_bytes") or 131072), 1), 524288)
        data = path.read_bytes()
        if b"\x00" in data[:4096]:
            return {"status": "partial_success", **self._metadata(path), "message": "Binary file metadata returned; content was not decoded.", "artifacts": self._artifact(path, user, arguments=arguments)}
        if arguments.get("start_line"):
            ranged = read_text_range(path, int(arguments["start_line"]), int(arguments.get("end_line") or 0))
            return {"status": "success", **self._metadata(path), **ranged,
                    "truncated": ranged["has_more"], "artifacts": self._artifact(path, user, ranged["content"], arguments)}
        content = data[:maximum].decode("utf-8", errors="replace")
        return {"status": "success", **self._metadata(path), "content": content, "truncated": len(data) > maximum, "artifacts": self._artifact(path, user, content, arguments)}

    def batch_read(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        """Bounded multi-file read so one audit does not spend an agent round per tiny file."""
        requests = arguments.get("files") or []
        if not isinstance(requests, list) or not requests:
            raise ValueError("files must be a non-empty array of read requests.")
        results: list[dict[str, Any]] = []
        total_chars = 0
        for item in requests[:BATCH_READ_MAX_FILES]:
            raw = str((item or {}).get("path") or "")
            entry: dict[str, Any] = {"path": raw}
            try:
                path = self._read_path(raw, user)
                if not path.is_file():
                    entry.update({"status": "no_result"})
                elif b"\x00" in path.read_bytes()[:4096]:
                    entry.update({"status": "partial_success", "message": "Binary file skipped in batch read."})
                else:
                    start = int((item or {}).get("start_line") or 1)
                    end = int((item or {}).get("end_line") or 0)
                    ranged = read_text_range(path, start, end if end else start + RANGED_READ_MAX_LINES - 1)
                    content = ranged["content"][:BATCH_READ_FILE_CHAR_LIMIT]
                    entry.update({"status": "success", **ranged, "content": content})
            except (SensitivePathError, ValueError) as error:
                entry.update({"status": "permission_denied", "message": str(error)[:300]})
            except OSError as error:
                entry.update({"status": "invalid_arguments", "message": str(error)[:300]})
            total_chars += len(str(entry.get("content") or ""))
            if total_chars > BATCH_READ_TOTAL_CHAR_LIMIT:
                entry["content"] = str(entry.get("content") or "")[: max(0, BATCH_READ_TOTAL_CHAR_LIMIT - (total_chars - len(str(entry.get("content") or ""))))]
                entry["truncated"] = True
                results.append(entry)
                break
            results.append(entry)
        return {"status": "success", "files": results, "files_requested": len(requests), "files_returned": len(results)}

    def write(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        path = self._managed_path(str(arguments.get("path") or ""), user)
        if path.exists():
            raise ValueError("Managed file already exists; use modify with its expected SHA256.")
        content = str(arguments.get("content") or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        workspace = "xoduz-sandbox" if self._is_authenticated_owner(user) else "user-managed"
        return {
            "status": "success", "operation": "write",
            "message": f"Created {path.name} in {workspace}.",
            "workspace": workspace, "workspace_root": str(self._mutation_root(user)),
            "receipt": {**self._metadata(path), "user_id": user["id"]},
            "artifacts": self._artifact(path, user, content),
        }

    def modify(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        path = self._managed_path(str(arguments.get("path") or ""), user)
        if not path.is_file():
            return {"status": "no_result", "path": str(path)}
        before = self._metadata(path)
        expected = str(arguments.get("expected_sha256") or "")
        if before["sha256"] != expected:
            raise ValueError("File changed before modification; expected SHA256 does not match.")
        path.write_text(str(arguments.get("content") or ""), encoding="utf-8", newline="\n")
        content = str(arguments.get("content") or "")
        workspace = "xoduz-sandbox" if self._is_authenticated_owner(user) else "user-managed"
        return {
            "status": "success", "operation": "modify",
            "message": f"Updated {path.name} in {workspace}.",
            "workspace": workspace, "workspace_root": str(self._mutation_root(user)),
            "receipt": {"before_sha256": before["sha256"], **self._metadata(path), "user_id": user["id"]},
            "artifacts": self._artifact(path, user, content),
        }
