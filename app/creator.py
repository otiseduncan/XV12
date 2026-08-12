from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen

import websockets
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from .artifacts import ArtifactStore, active_conversation_id
from .auth import current_user
from .builder_execution import BuilderExecutionService, parse_task_state
from .comfyui import ComfyUIConfig, ComfyUIProvider


FINAL_JOB_STATES = {"succeeded", "failed", "cancelled"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str, fallback: str = "item") -> str:
    return SAFE_NAME.sub("-", value.strip()).strip("-.")[:80] or fallback


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CreatorStore:
    """Durable, user-scoped creator state. Secret values are never stored here."""

    def __init__(self, path: Path, workspace_root: Path) -> None:
        self.path = path.resolve()
        self.workspace_root = workspace_root.resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS creator_workspaces (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,path TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS creator_workspaces_user ON creator_workspaces(user_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS creator_jobs (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,conversation_id TEXT NOT NULL,job_type TEXT NOT NULL,
                    state TEXT NOT NULL,progress INTEGER NOT NULL,message TEXT NOT NULL,workspace_id TEXT NOT NULL,
                    input_json TEXT NOT NULL,result_json TEXT NOT NULL,error_code TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,started_at TEXT,
                    completed_at TEXT,updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS creator_jobs_user ON creator_jobs(user_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS creator_previews (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,workspace_id TEXT NOT NULL,container_ref TEXT NOT NULL,
                    port INTEGER NOT NULL,url TEXT NOT NULL,state TEXT NOT NULL,access_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS builder_execution_sessions (
                    id TEXT PRIMARY KEY,user_id TEXT NOT NULL,conversation_id TEXT NOT NULL,project_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,parent_session_id TEXT NOT NULL,mode TEXT NOT NULL,
                    original_request TEXT NOT NULL,status TEXT NOT NULL,stage TEXT NOT NULL,job_id TEXT NOT NULL,
                    operation_count INTEGER NOT NULL DEFAULT 0,model_rounds INTEGER NOT NULL DEFAULT 0,
                    repair_cycles INTEGER NOT NULL DEFAULT 0,browser_cycles INTEGER NOT NULL DEFAULT 0,
                    elapsed_seconds REAL NOT NULL DEFAULT 0,generated_context_size INTEGER NOT NULL DEFAULT 0,
                    latest_observation_json TEXT NOT NULL DEFAULT '[]',preview_id TEXT NOT NULL DEFAULT '',
                    artifact_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS builder_sessions_conversation
                  ON builder_execution_sessions(user_id,conversation_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS creator_secret_refs (
                    name TEXT PRIMARY KEY,environment_name TEXT NOT NULL,contexts_json TEXT NOT NULL,
                    configured_by TEXT NOT NULL,updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creator_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO creator_meta(key,value) VALUES('schema_version','2')
                  ON CONFLICT(key) DO UPDATE SET value=excluded.value;
                """
            )
            preview_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(creator_previews)").fetchall()}
            if "access_token" not in preview_columns:
                db.execute("ALTER TABLE creator_previews ADD COLUMN access_token TEXT NOT NULL DEFAULT ''")
            session_columns = {str(row["name"]) for row in db.execute("PRAGMA table_info(builder_execution_sessions)").fetchall()}
            if "task_state_json" not in session_columns:
                db.execute("ALTER TABLE builder_execution_sessions ADD COLUMN task_state_json TEXT NOT NULL DEFAULT ''")
            db.execute(
                """UPDATE creator_jobs SET state='failed',progress=100,error_code='service_restarted',
                   message='The creator service restarted before this job finished.',completed_at=?,updated_at=?
                   WHERE state IN ('queued','running','cancelling')""",
                (utcnow(), utcnow()),
            )
            db.execute(
                """UPDATE builder_execution_sessions SET status='interrupted',
                   stage='Interrupted by service restart; workspace preserved',updated_at=?
                   WHERE status IN ('queued','running','cancelling')""",
                (utcnow(),),
            )

    def user_root(self, user_id: str) -> Path:
        root = (self.workspace_root / _digest(user_id)[:20]).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def create_workspace(self, user_id: str, name: str) -> dict[str, Any]:
        workspace_id = str(uuid.uuid4())
        path = (self.user_root(user_id) / f"{_slug(name, 'application')}-{workspace_id[:8]}").resolve()
        path.mkdir(parents=False, exist_ok=False)
        now = utcnow()
        with self.connect() as db:
            db.execute(
                "INSERT INTO creator_workspaces VALUES(?,?,?,?,?,?,?)",
                (workspace_id, user_id, name[:120], str(path), "active", now, now),
            )
        return self.workspace_public(self.workspace(workspace_id, user_id) or {})

    def open_workspace(self, user_id: str, workspace_id: str) -> dict[str, Any] | None:
        item = self.workspace(workspace_id, user_id)
        if not item or not Path(str(item["path"])).is_dir():
            return None
        with self.connect() as db:
            db.execute("UPDATE creator_workspaces SET updated_at=? WHERE id=?", (utcnow(), workspace_id))
        return self.workspace_public(item)

    def workspace(self, workspace_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM creator_workspaces WHERE id=? AND user_id=? AND status='active'",
                (workspace_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def workspace_public(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"), "name": item.get("name"), "status": item.get("status"),
            "reference": f"builder://workspace/{item.get('id')}", "updated_at": item.get("updated_at"),
        }

    def safe_path(self, workspace_id: str, user_id: str, relative: str = ".", *, must_exist: bool = False) -> Path:
        item = self.workspace(workspace_id, user_id)
        if not item:
            raise ValueError("Workspace was not found or is not owned by this user.")
        root = Path(str(item["path"])).resolve()
        if Path(relative).is_absolute() or "\0" in relative:
            raise ValueError("Workspace paths must be relative.")
        target = (root / relative).resolve()
        if target != root and not target.is_relative_to(root):
            raise ValueError("Path escapes the authorized Builder workspace.")
        if must_exist and not target.exists():
            raise ValueError("Requested workspace path does not exist.")
        return target

    def create_job(self, user_id: str, conversation_id: str, job_type: str, workspace_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        job_id, now = str(uuid.uuid4()), utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO creator_jobs(id,user_id,conversation_id,job_type,state,progress,message,workspace_id,
                   input_json,result_json,error_code,cancel_requested,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, user_id, conversation_id, job_type, "queued", 0, "Queued", workspace_id,
                 json.dumps(inputs), "{}", "", 0, now, now),
            )
        return self.job(job_id, user_id) or {}

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {"state", "progress", "message", "result_json", "error_code", "cancel_requested", "started_at", "completed_at"}
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as db:
            db.execute(f"UPDATE creator_jobs SET {assignments} WHERE id=?", (*updates.values(), job_id))

    def job(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM creator_jobs WHERE id=? AND user_id=?", (job_id, user_id)).fetchone()
        return dict(row) if row else None

    def cancel_job(self, job_id: str, user_id: str) -> dict[str, Any] | None:
        item = self.job(job_id, user_id)
        if not item:
            return None
        if item["state"] not in FINAL_JOB_STATES:
            self.update_job(job_id, cancel_requested=1, state="cancelling", message="Cancellation requested")
        return self.job(job_id, user_id)

    @staticmethod
    def job_public(item: dict[str, Any]) -> dict[str, Any]:
        try:
            result = json.loads(item.get("result_json") or "{}")
        except json.JSONDecodeError:
            result = {}
        try:
            inputs = json.loads(item.get("input_json") or "{}")
        except json.JSONDecodeError:
            inputs = {}
        return {
            "job_id": item.get("id"), "job_type": item.get("job_type"), "state": item.get("state"),
            "title": str(inputs.get("request") or item.get("job_type") or "Creator job")[:120],
            "progress": int(item.get("progress") or 0), "message": item.get("message"),
            "workspace_id": item.get("workspace_id") or None, "result": result,
            "error_code": item.get("error_code") or None, "created_at": item.get("created_at"),
            "started_at": item.get("started_at"), "completed_at": item.get("completed_at"),
        }

    def set_preview(
        self, preview_id: str, user_id: str, workspace_id: str, container: str, port: int, url: str,
        access_token: str = "",
    ) -> str:
        now = utcnow()
        access_token = access_token or secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute(
                """INSERT INTO creator_previews(
                   id,user_id,workspace_id,container_ref,port,url,state,access_token,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (preview_id, user_id, workspace_id, container, port, url, "running", access_token, now, now),
            )
        return access_token

    def preview(self, preview_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM creator_previews WHERE id=? AND user_id=?", (preview_id, user_id)).fetchone()
        return dict(row) if row else None

    def preview_by_token(self, preview_id: str, access_token: str) -> dict[str, Any] | None:
        if len(access_token) < 32:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM creator_previews WHERE id=? AND access_token=?", (preview_id, access_token)
            ).fetchone()
        return dict(row) if row else None

    def update_preview(self, preview_id: str, state: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE creator_previews SET state=?,updated_at=? WHERE id=?", (state, utcnow(), preview_id))

    def running_previews(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM creator_previews WHERE state='running'").fetchall()
        return [dict(row) for row in rows]

    def create_builder_session(
        self, *, user_id: str, conversation_id: str, workspace_id: str, request: str,
        mode: str, project_id: str = "", parent_session_id: str = "",
    ) -> dict[str, Any]:
        session_id, now = str(uuid.uuid4()), utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO builder_execution_sessions(
                   id,user_id,conversation_id,project_id,workspace_id,parent_session_id,mode,original_request,
                   status,stage,job_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, user_id, conversation_id, project_id[:100], workspace_id, parent_session_id[:100],
                 mode, request[:12000], "queued", "Queued", "", now, now),
            )
        return self.builder_session(session_id, user_id) or {}

    def builder_session(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM builder_execution_sessions WHERE id=? AND user_id=?", (session_id, user_id)
            ).fetchone()
        return dict(row) if row else None

    def latest_builder_session(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM builder_execution_sessions WHERE user_id=? AND conversation_id=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (user_id, conversation_id),
            ).fetchone()
        return dict(row) if row else None

    def update_builder_session(self, session_id: str, **changes: Any) -> None:
        allowed = {
            "status", "stage", "job_id", "operation_count", "model_rounds", "repair_cycles",
            "browser_cycles", "elapsed_seconds", "generated_context_size", "latest_observation_json",
            "preview_id", "artifact_id", "task_state_json",
        }
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = utcnow()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.connect() as db:
            db.execute(f"UPDATE builder_execution_sessions SET {assignments} WHERE id=?", (*updates.values(), session_id))

    @staticmethod
    def builder_session_public(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id"), "status": item.get("status"), "stage": item.get("stage"),
            "workspace_id": item.get("workspace_id"), "project_id": item.get("project_id") or None,
            "parent_session_id": item.get("parent_session_id") or None, "mode": item.get("mode"),
            "job_id": item.get("job_id") or None, "operation_count": int(item.get("operation_count") or 0),
            "model_rounds": int(item.get("model_rounds") or 0), "repair_cycles": int(item.get("repair_cycles") or 0),
            "browser_cycles": int(item.get("browser_cycles") or 0),
            "elapsed_seconds": float(item.get("elapsed_seconds") or 0),
            "generated_context_size": int(item.get("generated_context_size") or 0),
            "preview_id": item.get("preview_id") or None, "artifact_id": item.get("artifact_id") or None,
            "task_state": parse_task_state(item.get("task_state_json")),
            "updated_at": item.get("updated_at"),
        }

    def configure_secret_ref(self, name: str, environment_name: str, contexts: list[str], admin_id: str) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as db:
            db.execute(
                """INSERT INTO creator_secret_refs VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                   environment_name=excluded.environment_name,contexts_json=excluded.contexts_json,
                   configured_by=excluded.configured_by,updated_at=excluded.updated_at""",
                (_slug(name, "secret"), environment_name, json.dumps(sorted(set(contexts))), admin_id, now),
            )
        return self.secret_status(name)

    def secret_status(self, name: str = "") -> dict[str, Any]:
        with self.connect() as db:
            if name:
                rows = db.execute("SELECT name,environment_name,contexts_json,updated_at FROM creator_secret_refs WHERE name=?", (_slug(name),)).fetchall()
            else:
                rows = db.execute("SELECT name,environment_name,contexts_json,updated_at FROM creator_secret_refs ORDER BY name").fetchall()
        refs = [{
            "name": row["name"], "configured": bool(os.environ.get(str(row["environment_name"]))),
            "contexts": json.loads(row["contexts_json"]), "updated_at": row["updated_at"],
        } for row in rows]
        return refs[0] if name and refs else {"name": _slug(name), "configured": False, "contexts": []} if name else {"references": refs}

    def resolve_secrets(self, names: list[str], context: str) -> dict[str, str]:
        result: dict[str, str] = {}
        with self.connect() as db:
            for name in names:
                row = db.execute("SELECT * FROM creator_secret_refs WHERE name=?", (_slug(name),)).fetchone()
                if not row or context not in json.loads(row["contexts_json"]):
                    raise ValueError(f"Secret reference {name} is not authorized for {context}.")
                value = os.environ.get(str(row["environment_name"]))
                if not value:
                    raise ValueError(f"Secret reference {name} is not configured.")
                result[_slug(name).upper()] = value
        return result


class JobManager:
    def __init__(self, store: CreatorStore) -> None:
        self.store = store
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="xv12-creator")

    def submit(
        self, user_id: str, conversation_id: str, job_type: str, workspace_id: str,
        inputs: dict[str, Any], worker: Callable[[str, Callable[[int, str], None], Callable[[], bool]], dict[str, Any]],
    ) -> dict[str, Any]:
        item = self.store.create_job(user_id, conversation_id, job_type, workspace_id, inputs)
        job_id = str(item["id"])

        def run() -> None:
            self.store.update_job(job_id, state="running", progress=1, message="Started", started_at=utcnow())

            def progress(value: int, message: str) -> None:
                self.store.update_job(job_id, progress=max(0, min(int(value), 99)), message=message[:300])

            def cancelled() -> bool:
                current = self.store.job(job_id, user_id)
                return bool(current and current.get("cancel_requested"))

            try:
                result = worker(job_id, progress, cancelled)
                if cancelled():
                    self.store.update_job(
                        job_id, state="cancelled", progress=100, message="Cancelled; workspace preserved",
                        result_json=json.dumps(result), completed_at=utcnow(),
                    )
                elif isinstance(result, dict) and result.get("status") in {"partial_success", "execution_error"}:
                    self.store.update_job(
                        job_id, state="failed", progress=100,
                        message=str(result.get("message") or "Job stopped safely.")[:300],
                        result_json=json.dumps(result), error_code=str(result.get("status")), completed_at=utcnow(),
                    )
                else:
                    self.store.update_job(
                        job_id, state="succeeded", progress=100,
                        message=str(result.get("message") or "Complete")[:300] if isinstance(result, dict) else "Complete",
                        result_json=json.dumps(result), completed_at=utcnow(),
                    )
            except Exception as error:
                self.store.update_job(
                    job_id, state="failed", progress=100, message="Job failed safely.",
                    error_code=type(error).__name__, completed_at=utcnow(),
                )

        self.executor.submit(run)
        return CreatorStore.job_public(item)


class SecretsBroker:
    def __init__(self, store: CreatorStore) -> None:
        self.store = store

    def configure(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if user["role"] != "admin":
            return {"status": "permission_denied", "message": "Administrator role required."}
        ref = self.store.configure_secret_ref(
            str(arguments["name"]), str(arguments["environment_name"]),
            [str(item) for item in arguments.get("contexts") or []], user["id"],
        )
        return {"status": "success", "reference": ref, "secret_value_exposed": False}

    def status(self, arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        return {"status": "success", **self.store.secret_status(str(arguments.get("name") or "")), "secret_value_exposed": False}


class WorkspaceService:
    def __init__(self, store: CreatorStore, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts

    def create(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace = self.store.create_workspace(user["id"], str(arguments["name"]))
        return {"status": "success", "workspace": workspace}

    def open(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace = self.store.open_workspace(user["id"], str(arguments["workspace_id"]))
        return {"status": "success", "workspace": workspace} if workspace else {"status": "no_result", "message": "Workspace not found."}

    def inspect(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)
        files: list[str] = []
        ignored = {".git", "node_modules", ".creator-deps", "__pycache__"}
        for path in root.rglob("*"):
            if any(part in ignored for part in path.parts) or not path.is_file():
                continue
            files.append(path.relative_to(root).as_posix())
            if len(files) >= 300:
                break
        manifests = [name for name in ("package.json", "requirements.txt", "pyproject.toml", "Dockerfile") if (root / name).is_file()]
        tests = [name for name in files if "test" in name.casefold()][:30]
        return {"status": "success", "workspace": self.store.workspace_public(self.store.workspace(workspace_id, user["id"]) or {}),
                "file_count_returned": len(files), "files": files, "manifests": manifests, "test_files": tests}

    def read(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        path = self.store.safe_path(str(arguments["workspace_id"]), user["id"], str(arguments["path"]), must_exist=True)
        if not path.is_file() or path.stat().st_size > 256_000:
            raise ValueError("Builder reads are limited to files of 256 KB or less.")
        data = path.read_text(encoding="utf-8")
        return {"status": "success", "path": str(arguments["path"]), "content": data, "sha256": hashlib.sha256(data.encode()).hexdigest()}

    def patch(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id, relative = str(arguments["workspace_id"]), str(arguments["path"])
        path = self.store.safe_path(workspace_id, user["id"], relative)
        content = str(arguments["content"])
        if len(content.encode("utf-8")) > 512_000:
            raise ValueError("A Builder file write is limited to 512 KB.")
        expected = str(arguments.get("expected_sha256") or "")
        if expected:
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
            if current != expected:
                raise ValueError("The file changed since it was read; the patch was not applied.")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
        return {"status": "success", "changed": True, "path": relative,
                "sha256": hashlib.sha256(content.encode()).hexdigest(), "bytes": len(content.encode())}

    def batch(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        items = arguments.get("files") or []
        if not items or len(items) > 100:
            raise ValueError("Atomic batches require 1 to 100 files.")
        total = sum(len(str(item.get("content") or "").encode()) for item in items)
        if total > 3_000_000:
            raise ValueError("Atomic batch payload exceeds 3 MB.")
        workspace_id = str(arguments["workspace_id"])
        targets = [(self.store.safe_path(workspace_id, user["id"], str(item["path"])), str(item["path"]), str(item.get("content") or "")) for item in items]
        if len({path for path, _, _ in targets}) != len(targets):
            raise ValueError("Atomic batch contains duplicate paths.")
        originals = {path: path.read_bytes() if path.is_file() else None for path, _, _ in targets}
        try:
            for path, _, content in targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(content, encoding="utf-8", newline="\n")
                temporary.replace(path)
        except Exception:
            for path, content in originals.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            raise
        return {"status": "success", "atomic": True, "files_written": len(targets), "bytes_written": total,
                "paths": [relative for _, relative, _ in targets]}

    _CODE_SEARCH_IGNORED_DIRS = {".git", "node_modules", ".creator-deps", ".xv12-artifacts", "__pycache__", ".pytest_cache", "dist", "build"}
    _CODE_SEARCH_MAX_FILE_BYTES = 750_000
    _CODE_SEARCH_MAX_FILES_SCANNED = 4000
    _CODE_SEARCH_LINE_CHARS = 200

    def code_search(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        """Bounded workspace-only search for text, regex, or filenames. Deliberately a plain
        Python scan rather than a subprocess/ripgrep dependency, matching how inspect()/archive()
        already walk the workspace tree in-process. Cheap and intentional, not a semantic index."""
        workspace_id = str(arguments["workspace_id"])
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise ValueError("A search query is required.")
        mode = str(arguments.get("mode") or "text")
        if mode not in {"text", "regex", "filename"}:
            raise ValueError("mode must be text, regex, or filename.")
        limit = max(1, min(int(arguments.get("limit") or 25), 60))
        path_glob = str(arguments.get("path_glob") or "").strip()
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)

        pattern: re.Pattern[str] | None = None
        if mode == "regex":
            try:
                pattern = re.compile(query)
            except re.error as error:
                raise ValueError(f"Invalid regular expression: {error}") from error

        matches: list[dict[str, Any]] = []
        files_scanned = 0
        for path in sorted(root.rglob("*")):
            if len(matches) >= limit or files_scanned >= self._CODE_SEARCH_MAX_FILES_SCANNED:
                break
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in self._CODE_SEARCH_IGNORED_DIRS for part in relative.parts):
                continue
            rel_posix = relative.as_posix()
            if path_glob and not fnmatch.fnmatch(rel_posix, path_glob):
                continue
            if mode == "filename":
                files_scanned += 1
                if query.casefold() in rel_posix.casefold():
                    matches.append({"path": rel_posix, "line": 0, "text": rel_posix})
                continue
            try:
                if path.stat().st_size > self._CODE_SEARCH_MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files_scanned += 1
            for line_number, line in enumerate(text.splitlines(), start=1):
                hit = bool(pattern.search(line)) if pattern else (query.casefold() in line.casefold())
                if not hit:
                    continue
                matches.append({"path": rel_posix, "line": line_number, "text": line.strip()[: self._CODE_SEARCH_LINE_CHARS]})
                if len(matches) >= limit:
                    break
        return {
            "status": "success" if matches else "no_result",
            "query": query, "mode": mode, "matches": matches, "match_count": len(matches),
            "files_scanned": files_scanned, "truncated": len(matches) >= limit,
        }

    _CODE_MAP_IGNORED_DIRS = {".git", "node_modules", ".creator-deps", ".xv12-artifacts", "__pycache__", ".pytest_cache", "dist", "build", ".next", ".venv"}
    _CODE_MAP_SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
    _CODE_MAP_STYLE_SUFFIXES = {".css", ".scss", ".less"}
    _CODE_MAP_CONFIG_NAMES = {
        "package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "Dockerfile",
        "docker-compose.yml", "tsconfig.json", "vite.config.js", "vite.config.ts", "webpack.config.js",
        ".env.example", "next.config.js", "tailwind.config.js",
    }
    _CODE_MAP_ENTRY_NAMES = {
        "main.py", "app.py", "wsgi.py", "asgi.py", "__init__.py", "index.js", "index.ts",
        "index.html", "server.js", "app.js", "App.jsx", "App.tsx", "main.js", "main.ts",
    }
    _CODE_MAP_MAX_FILES = 4000
    _CODE_MAP_MAX_LIST = 40
    _CODE_MAP_IMPORT_SAMPLE_FILES = 20
    _CODE_MAP_IMPORT_SAMPLE_TOTAL = 120
    _CODE_MAP_IMPORT_LINE_RE = re.compile(
        r'^\s*(?:import\s+.+|from\s+\S+\s+import\s+.+|export\s+.+from\s+["\'].+["\'])', re.MULTILINE,
    )

    def code_map(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        """Compact, cheap project map: manifests, likely entry points, source/component
        directories, tests, styles, routing files, and a bounded import/export sample.
        Deliberately simple -- not a semantic index or LSP; add that only if evidence shows
        this bounded scan is insufficient for orientation."""
        workspace_id = str(arguments["workspace_id"])
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)

        manifests: list[str] = []
        entry_points: list[str] = []
        tests: list[str] = []
        styles: list[str] = []
        routing: list[str] = []
        component_dirs: set[str] = set()
        source_dirs: set[str] = set()
        import_samples: list[dict[str, str]] = []
        files_import_scanned = 0
        total_files = 0

        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(part in self._CODE_MAP_IGNORED_DIRS for part in relative.parts):
                continue
            total_files += 1
            if total_files > self._CODE_MAP_MAX_FILES:
                break
            rel_posix = relative.as_posix()
            name = path.name
            suffix = path.suffix.casefold()
            lowered = rel_posix.casefold()

            if name in self._CODE_MAP_CONFIG_NAMES and len(manifests) < self._CODE_MAP_MAX_LIST:
                manifests.append(rel_posix)
            if name in self._CODE_MAP_ENTRY_NAMES and len(entry_points) < self._CODE_MAP_MAX_LIST:
                entry_points.append(rel_posix)
            if "test" in lowered and len(tests) < self._CODE_MAP_MAX_LIST:
                tests.append(rel_posix)
            if suffix in self._CODE_MAP_STYLE_SUFFIXES and len(styles) < self._CODE_MAP_MAX_LIST:
                styles.append(rel_posix)
            if ("route" in lowered or "router" in lowered) and len(routing) < self._CODE_MAP_MAX_LIST:
                routing.append(rel_posix)
            if "components" in relative.parts:
                index = relative.parts.index("components")
                component_dirs.add(Path(*relative.parts[: index + 1]).as_posix())
            if suffix in self._CODE_MAP_SOURCE_SUFFIXES and len(relative.parts) > 1:
                source_dirs.add(relative.parts[0])

            if (
                suffix in self._CODE_MAP_SOURCE_SUFFIXES
                and files_import_scanned < self._CODE_MAP_IMPORT_SAMPLE_FILES
                and len(import_samples) < self._CODE_MAP_IMPORT_SAMPLE_TOTAL
            ):
                try:
                    if path.stat().st_size <= 200_000:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        files_import_scanned += 1
                        found = 0
                        for match in self._CODE_MAP_IMPORT_LINE_RE.finditer(text):
                            if found >= 6 or len(import_samples) >= self._CODE_MAP_IMPORT_SAMPLE_TOTAL:
                                break
                            import_samples.append({"path": rel_posix, "statement": match.group(0).strip()[:160]})
                            found += 1
                except OSError:
                    pass

        return {
            "status": "success",
            "workspace_id": workspace_id,
            "manifests": manifests,
            "entry_points": entry_points,
            "source_dirs": sorted(source_dirs)[: self._CODE_MAP_MAX_LIST],
            "component_dirs": sorted(component_dirs)[: self._CODE_MAP_MAX_LIST],
            "test_files": tests,
            "style_files": styles,
            "routing_files": routing,
            "import_export_samples": import_samples,
            "files_scanned": min(total_files, self._CODE_MAP_MAX_FILES),
            "truncated": total_files > self._CODE_MAP_MAX_FILES,
        }

    def archive(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)
        output_dir = root / ".xv12-artifacts"
        output_dir.mkdir(exist_ok=True)
        target = output_dir / f"{_slug(root.name)}-{uuid.uuid4().hex[:8]}.zip"
        excluded_dirs = {".git", "node_modules", ".creator-deps", ".xv12-artifacts", "__pycache__"}
        excluded_names = {".env", ".env.local", ".env.production"}
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                relative = path.relative_to(root)
                if not path.is_file() or any(part in excluded_dirs for part in relative.parts) or path.name in excluded_names:
                    continue
                archive.write(path, relative.as_posix())
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id="builder.project.archive", source_path=target,
            title=f"{root.name} project archive", source_label="XV12 Builder", conversation_id=str(arguments.get("conversation_id") or active_conversation_id() or "creator"),
            artifact_type="project_archive", actions=["download"], metadata={"workspace_id": workspace_id, "secret_files_excluded": True},
        )
        return {"status": "success", "artifact": artifact, "workspace_id": workspace_id}


class SandboxService:
    """Runs argv inside a constrained Docker container with only the owned workspace mounted."""

    def __init__(self, store: CreatorStore, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts
        self.image = os.environ.get("XV12_BUILDER_IMAGE", "python:3.12-alpine")

    @staticmethod
    def available() -> bool:
        return shutil.which("docker") is not None

    def execute(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)
        argv = arguments.get("argv") or []
        if not argv or len(argv) > 80 or any(not isinstance(item, str) or "\0" in item or len(item) > 4000 for item in argv):
            raise ValueError("Sandbox argv must contain 1 to 80 bounded string arguments.")
        timeout = min(max(int(arguments.get("timeout_seconds") or 120), 1), 900)
        network = bool(arguments.get("network"))
        secrets = self.store.resolve_secrets([str(x) for x in arguments.get("secret_refs") or []], "builder")
        command = [
            "docker", "run", "--rm", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "128",
            "--memory", "768m", "--cpus", "1.5", "--network", "bridge" if network else "none",
            "--mount", f"type=bind,source={root},target=/workspace", "--workdir", "/workspace",
        ]
        secret_environment = {**os.environ, **secrets}
        for name in secrets:
            command.extend(["--env", name])
        command.extend([self.image, *argv])
        started = time.monotonic()
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, env=secret_environment)
        elapsed = round(time.monotonic() - started, 3)
        combined = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
        for value in secrets.values():
            combined = combined.replace(value, "[REDACTED]")
        receipt_dir = root / ".xv12-artifacts" / "receipts"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / f"sandbox-{uuid.uuid4().hex}.txt"
        receipt_path.write_text(combined[:2_000_000], encoding="utf-8")
        report_type = str(arguments.get("report_type") or "build_report")
        if report_type not in {"build_report", "test_report"}:
            report_type = "build_report"
        conversation = str(arguments.get("conversation_id") or active_conversation_id() or "creator")
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id="builder.sandbox.exec", source_path=receipt_path,
            title="Test execution receipt" if report_type == "test_report" else "Build execution receipt",
            source_label="XV12 isolated Builder sandbox", conversation_id=conversation, artifact_type=report_type,
            relevant_text=combined[:60_000], actions=["view", "download", "copy"],
            metadata={"workspace_id": workspace_id, "exit_code": completed.returncode, "duration_seconds": elapsed,
                      "network": "enabled" if network else "disabled", "secret_values_exposed": False},
        )
        return {
            "status": "success" if completed.returncode == 0 else "execution_error",
            "executed": True, "exit_code": completed.returncode, "duration_seconds": elapsed,
            "summary": combined[-8000:], "truncated": len(combined) > 8000,
            "sandbox": {"engine": "docker", "workspace_only_mount": True, "host_shell": False,
                        "network": "enabled" if network else "disabled", "limits_enforced": True},
            "artifact": artifact,
        }


class PreviewService:
    def __init__(self, store: CreatorStore, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts

    def _allocate_port(self) -> int:
        for port in range(18400, 18500):
            check = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--format", "{{.ID}}"], capture_output=True, text=True)
            if not check.stdout.strip():
                return port
        raise RuntimeError("No managed preview port is available.")

    @staticmethod
    def proxy_url(preview_id: str, access_token: str = "") -> str:
        return (
            f"/api/creator/previews/{preview_id}/token/{access_token}/"
            if access_token else f"/api/creator/previews/{preview_id}/"
        )

    def reconcile(self) -> dict[str, int]:
        checked = stopped = 0
        for preview in self.store.running_previews():
            checked += 1
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Running}}", str(preview["container_ref"])],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or result.stdout.strip() != "true":
                self.store.update_preview(str(preview["id"]), "stopped")
                stopped += 1
        return {"checked": checked, "marked_stopped": stopped}

    def start(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)
        relative = str(arguments.get("directory") or ".")
        serving = self.store.safe_path(workspace_id, user["id"], relative, must_exist=True)
        if not serving.is_dir():
            raise ValueError("Preview directory must be a workspace directory.")
        port, preview_id = self._allocate_port(), str(uuid.uuid4())
        label = f"xv12.preview={preview_id}"
        command = [
            "docker", "run", "-d", "--rm", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "64",
            "--memory", "256m", "--cpus", "0.5", "--network", "bridge", "--label", label,
            "-p", f"127.0.0.1:{port}:8080", "--mount", f"type=bind,source={serving},target=/workspace,readonly",
            "--workdir", "/workspace", "python:3.12-alpine", "python", "-m", "http.server", "8080", "--bind", "0.0.0.0",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise RuntimeError("The isolated preview container did not start.")
        container = result.stdout.strip()
        url = f"http://127.0.0.1:{port}/"
        access_token = self.store.set_preview(preview_id, user["id"], workspace_id, container, port, url)
        for _ in range(25):
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status < 500:
                        break
            except Exception:
                time.sleep(0.2)
        manifest = root / ".xv12-artifacts" / f"application-{preview_id}.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        proxy_url = self.proxy_url(preview_id, access_token)
        manifest.write_text(json.dumps({"preview_id": preview_id, "workspace_id": workspace_id}, indent=2), encoding="utf-8")
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id="builder.preview.start", source_path=manifest,
            title=str(arguments.get("title") or "Application preview"), source_label="XV12 Builder Preview",
            conversation_id=str(arguments.get("conversation_id") or active_conversation_id() or "creator"),
            artifact_type="application", actions=["open"],
            metadata={"preview_url": proxy_url, "preview_id": preview_id, "workspace_id": workspace_id,
                      "state": "running", "healthy": False, "managed_preview": True},
        )
        return {"status": "success", "preview": {"id": preview_id, "url": url, "proxy_url": proxy_url,
                "state": "running", "workspace_id": workspace_id}, "artifact": artifact}

    def finalize_artifact(
        self, *, preview_id: str, user: dict[str, Any], conversation_id: str, title: str,
        screenshot: dict[str, Any] | None, project_archive: dict[str, Any] | None,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        preview = self.store.preview(preview_id, user["id"])
        if not preview or preview["state"] != "running":
            raise ValueError("A running user-owned preview is required for finalization.")
        root = self.store.safe_path(str(preview["workspace_id"]), user["id"], ".", must_exist=True)
        manifest = root / ".xv12-artifacts" / f"application-{preview_id}.json"
        manifest.write_text(json.dumps({
            "preview_id": preview_id, "workspace_id": preview["workspace_id"],
            "healthy": bool(validation.get("healthy")), "validated_at": utcnow(),
        }, indent=2), encoding="utf-8")
        return self.artifacts.register_file(
            user_id=user["id"], capability_id="builder.preview.start", source_path=manifest,
            title=title or "Application preview", source_label="XV12 Verified Builder Preview",
            conversation_id=conversation_id, artifact_type="application", actions=["open"],
            metadata={
                "preview_url": self.proxy_url(preview_id, str(preview.get("access_token") or "")), "preview_id": preview_id,
                "workspace_id": preview["workspace_id"], "state": "running", "healthy": True,
                "managed_preview": True, "validated_at": utcnow(), "validation": validation,
                "screenshot": screenshot, "project_archive": project_archive,
            },
        )

    def status(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        preview = self.store.preview(str(arguments["preview_id"]), user["id"])
        if not preview:
            return {"status": "no_result", "message": "Preview not found."}
        result = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", str(preview["container_ref"])], capture_output=True, text=True)
        state = "running" if result.returncode == 0 and result.stdout.strip() == "true" else "stopped"
        self.store.update_preview(str(preview["id"]), state)
        return {"status": "success", "preview": {"id": preview["id"], "url": preview["url"], "state": state, "workspace_id": preview["workspace_id"]}}

    def stop(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        preview = self.store.preview(str(arguments["preview_id"]), user["id"])
        if not preview:
            return {"status": "no_result", "message": "Preview not found."}
        result = subprocess.run(["docker", "stop", "--time", "3", str(preview["container_ref"])], capture_output=True, text=True, timeout=15)
        self.store.update_preview(str(preview["id"]), "stopped")
        return {"status": "success", "stopped": result.returncode == 0, "preview_id": preview["id"]}


class BrowserService:
    def __init__(self, store: CreatorStore, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts
        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        self.browser = next((path for path in candidates if path.is_file()), None)

    def _authorized_url(self, preview_id: str, user_id: str) -> tuple[dict[str, Any], str]:
        preview = self.store.preview(preview_id, user_id)
        if not preview or preview["state"] != "running":
            raise ValueError("A running user-owned preview is required.")
        parsed = urlparse(str(preview["url"]))
        if parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Browser validation is limited to managed local previews.")
        return preview, str(preview["url"])

    # Per-visible-element computed-style telemetry, evaluated inside Chromium after the
    # document snapshot. Bounded (element cap, sliced strings) and fail-soft: the JS returns
    # {error} on any throw, and default-valued keys are omitted entirely to keep the payload
    # compact -- see defaults_omitted in the returned object for the convention.
    _STYLE_TELEMETRY_JS = """(()=>{try{
const out=[];const vw=innerWidth,vh=innerHeight;const skip={SCRIPT:1,STYLE:1,LINK:1,META:1,NOSCRIPT:1,TEMPLATE:1};
for(const el of document.querySelectorAll('body, body *')){
 if(out.length>=80)break;
 if(skip[el.tagName])continue;
 const cs=getComputedStyle(el);
 if(cs.display==='none'||cs.visibility==='hidden')continue;
 const r=el.getBoundingClientRect();
 if(r.width<2||r.height<2)continue;
 const sel=(el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(el.classList.length?'.'+[...el.classList].slice(0,3).join('.'):'')).slice(0,120);
 let covered;
 if(r.top<vh&&r.bottom>0&&r.left<vw&&r.right>0){
  const hit=document.elementFromPoint(Math.max(0,Math.min(vw-1,r.left+r.width/2)),Math.max(0,Math.min(vh-1,r.top+r.height/2)));
  if(hit&&hit!==el&&!el.contains(hit)&&!hit.contains(el))covered=true;
 }
 const item={sel,rect:{x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)},
  bg:cs.backgroundColor,color:cs.color,
  font:{family:cs.fontFamily.slice(0,60),size:cs.fontSize,weight:cs.fontWeight,lineHeight:cs.lineHeight},
  pad:cs.padding,margin:cs.margin,display:cs.display,
  border:(cs.border||cs.borderTopWidth+' '+cs.borderTopStyle+' '+cs.borderTopColor).slice(0,80)};
 if(cs.backdropFilter&&cs.backdropFilter!=='none')item.backdrop=cs.backdropFilter.slice(0,80);
 if(cs.boxShadow!=='none')item.shadow=cs.boxShadow.slice(0,120);
 if(cs.borderRadius!=='0px')item.radius=cs.borderRadius.slice(0,60);
 if(cs.display.includes('flex'))item.flex={dir:cs.flexDirection,justify:cs.justifyContent,align:cs.alignItems,wrap:cs.flexWrap};
 if(cs.display.includes('grid'))item.grid={cols:cs.gridTemplateColumns.slice(0,120),rows:cs.gridTemplateRows.slice(0,120)};
 if(cs.gap!=='normal'&&cs.gap!=='0px')item.gap=cs.gap;
 if(cs.overflow!=='visible')item.overflow=cs.overflow;
 if(cs.zIndex!=='auto')item.z=cs.zIndex;
 if(r.right<0||r.bottom<0||r.left>vw||r.top>vh)item.offscreen=true;
 else if(r.left<-1||r.top<-1||r.right>vw+1)item.clipped=true;
 if(covered)item.covered=true;
 out.push(item);
}
return{viewport:{w:vw,h:vh,scrollW:document.documentElement.scrollWidth,scrollH:document.documentElement.scrollHeight,
 horizontalOverflow:document.documentElement.scrollWidth>vw},
 defaults_omitted:'backdrop=none shadow=none radius=0px gap=0 overflow=visible z=auto offscreen/clipped/covered=false',
 elements:out};
}catch(e){return{error:String(e).slice(0,300)}}})()"""

    @staticmethod
    def _collect_devtools(websocket_url: str, url: str, click_selector: str = "") -> dict[str, Any]:
        async def collect() -> dict[str, Any]:
            console: list[dict[str, str]] = []
            runtime_errors: list[str] = []
            network_failures: list[dict[str, Any]] = []

            def observe(message: dict[str, Any]) -> None:
                method, params = message.get("method"), message.get("params") or {}
                if method == "Runtime.consoleAPICalled":
                    values = [str(item.get("value") if "value" in item else item.get("description") or item.get("type") or "") for item in params.get("args") or []]
                    console.append({"level": str(params.get("type") or "log"), "text": " ".join(values)[:1000]})
                elif method == "Runtime.exceptionThrown":
                    details = params.get("exceptionDetails") or {}
                    exception = details.get("exception") or {}
                    runtime_errors.append(str(exception.get("description") or details.get("text") or "JavaScript exception")[:2000])
                elif method == "Log.entryAdded":
                    entry = params.get("entry") or {}
                    entry_url = str(entry.get("url") or "")
                    if not urlparse(entry_url).path.endswith("/favicon.ico"):
                        console.append({"level": str(entry.get("level") or "log"), "text": str(entry.get("text") or "")[:1000], "url": entry_url[:500]})
                elif method == "Network.loadingFailed":
                    error_text = str(params.get("errorText") or "loading failed")
                    if error_text != "net::ERR_ABORTED":
                        network_failures.append({"url": str(params.get("blockedReason") or "request")[:500], "error": error_text[:500]})
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    response_url = str(response.get("url") or "")
                    if int(response.get("status") or 0) >= 400 and not urlparse(response_url).path.endswith("/favicon.ico"):
                        network_failures.append({"url": response_url[:500], "status": int(response["status"])})

            async with websockets.connect(websocket_url, max_size=8_000_000, open_timeout=10) as socket:
                sequence = 0

                async def command(method: str, params: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
                    nonlocal sequence
                    sequence += 1
                    request_id = sequence
                    await socket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.monotonic())))
                        observe(message)
                        if message.get("id") == request_id:
                            return message
                    raise TimeoutError(f"DevTools command timed out: {method}")

                for method in ("Page.enable", "Runtime.enable", "Log.enable", "Network.enable"):
                    await command(method)
                await command("Page.navigate", {"url": url}, timeout=15)
                settle_until = time.monotonic() + 1.5
                while time.monotonic() < settle_until:
                    try:
                        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=max(0.05, settle_until - time.monotonic())))
                        observe(message)
                    except asyncio.TimeoutError:
                        break
                click_result = None
                if click_selector:
                    expression = f"(()=>{{const e=document.querySelector({json.dumps(click_selector)});if(!e)return false;e.click();return true;}})()"
                    response = await command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                    click_result = bool((((response.get("result") or {}).get("result") or {}).get("value")))
                expression = "(()=>({title:document.title,body:(document.body?.innerText||'').slice(0,3000),dom:document.documentElement?.outerHTML||''}))()"
                response = await command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
                document = (((response.get("result") or {}).get("result") or {}).get("value")) or {}
                # Strictly additive: a telemetry failure must never invalidate the
                # DOM/console/network evidence already collected above.
                try:
                    response = await command("Runtime.evaluate", {"expression": BrowserService._STYLE_TELEMETRY_JS, "returnByValue": True})
                    telemetry = (((response.get("result") or {}).get("result") or {}).get("value")) or {}
                    if not isinstance(telemetry, dict):
                        telemetry = {"error": "style telemetry returned a non-object value"}
                except Exception as error:
                    telemetry = {"error": f"style telemetry unavailable: {str(error)[:200]}"}
            return {"document": document, "console": console[:50], "runtime_errors": runtime_errors[:25],
                    "network_failures": network_failures[:50], "click_performed": click_result,
                    "style_telemetry": telemetry}

        return asyncio.run(collect())

    def _inspect_once(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if not self.browser:
            return {"status": "unavailable", "message": "Chromium browser is not installed."}
        preview, url = self._authorized_url(str(arguments["preview_id"]), user["id"])
        with tempfile.TemporaryDirectory(prefix="xv12-browser-") as profile:
            process = subprocess.Popen(
                [str(self.browser), "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                 "--remote-debugging-port=0", "--remote-allow-origins=*", f"--user-data-dir={profile}", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                port_file = Path(profile) / "DevToolsActivePort"
                port = None
                for _ in range(100):
                    if port_file.is_file():
                        try:
                            lines = port_file.read_text(encoding="utf-8").splitlines()
                            if lines and lines[0].isdigit():
                                port = int(lines[0])
                                break
                        except OSError:
                            pass
                    if process.poll() is not None:
                        raise RuntimeError("Chromium stopped before DevTools became available.")
                    time.sleep(0.05)
                if port is None:
                    raise RuntimeError("Chromium DevTools port did not become available.")
                targets = []
                for _ in range(40):
                    try:
                        targets = json.loads(urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2).read())
                        if any(item.get("type") == "page" for item in targets):
                            break
                    except Exception:
                        time.sleep(0.05)
                page = next(item for item in targets if item.get("type") == "page")
                evidence = self._collect_devtools(str(page["webSocketDebuggerUrl"]), url, str(arguments.get("click_selector") or ""))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        document = evidence["document"]
        dom = str(document.get("dom") or "")
        return {"status": "success" if bool(dom) else "execution_error",
                "preview_id": preview["id"], "http_url": url, "browser": "Chromium DevTools",
                "title": str(document.get("title") or ""), "body_text": str(document.get("body") or ""),
                "dom_bytes": len(dom.encode()), "rendered": bool(dom), "console": evidence["console"],
                "runtime_errors": evidence["runtime_errors"], "network_failures": evidence["network_failures"],
                "console_inspected": True, "network_inspected": True, "click_performed": evidence["click_performed"],
                "style_telemetry": evidence.get("style_telemetry") or {},
                "healthy": not evidence["runtime_errors"] and not evidence["network_failures"] and not any(item.get("level") == "error" for item in evidence["console"])}

    def inspect(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if not self.browser:
            return {"status": "unavailable", "message": "Chromium browser is not installed."}
        last_error = ""
        for attempt in range(3):
            try:
                result = self._inspect_once(arguments, user)
                if result.get("status") == "success":
                    return {**result, "inspection_attempts": attempt + 1}
                last_error = str(result.get("message") or result.get("status") or "inspection failed")
            except Exception as error:
                last_error = type(error).__name__
            if attempt < 2:
                time.sleep(0.25 * (attempt + 1))
        return {"status": "execution_error", "message": "Chromium inspection failed after bounded retries.",
                "error": last_error, "inspection_attempts": 3}

    def screenshot(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if not self.browser:
            return {"status": "unavailable", "message": "Chromium browser is not installed."}
        preview, url = self._authorized_url(str(arguments["preview_id"]), user["id"])
        root = self.store.safe_path(str(preview["workspace_id"]), user["id"], ".", must_exist=True)
        target = root / ".xv12-artifacts" / f"screenshot-{uuid.uuid4().hex}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="xv12-browser-") as profile:
            result = subprocess.run(
                [str(self.browser), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                 f"--user-data-dir={profile}", "--window-size=1440,1000", f"--screenshot={target}", url],
                capture_output=True, text=True, timeout=60,
            )
        if result.returncode != 0 or not target.is_file() or target.stat().st_size < 1000:
            return {"status": "execution_error", "message": "Chromium did not produce a screenshot."}
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id="browser.preview.screenshot", source_path=target,
            title=str(arguments.get("title") or "Application screenshot"), source_label="XV12 Chromium validation",
            conversation_id=str(arguments.get("conversation_id") or active_conversation_id() or "creator"),
            artifact_type="screenshot", actions=["view", "download"],
            metadata={"preview_id": preview["id"], "workspace_id": preview["workspace_id"], "browser": "Chromium", "width": 1440, "height": 1000},
        )
        return {"status": "success", "rendered": True, "artifact": artifact, "preview_id": preview["id"]}


class GitService:
    def __init__(self, store: CreatorStore, artifacts: ArtifactStore) -> None:
        self.store, self.artifacts = store, artifacts

    def _run(self, workspace_id: str, user_id: str, argv: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        root = self.store.safe_path(workspace_id, user_id, ".", must_exist=True)
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
        return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=environment)

    def _receipt(self, capability_id: str, workspace_id: str, user: dict[str, Any], arguments: dict[str, Any], result: subprocess.CompletedProcess[str], operation: str) -> dict[str, Any]:
        root = self.store.safe_path(workspace_id, user["id"], ".", must_exist=True)
        text = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        receipt = root / ".xv12-artifacts" / "receipts" / f"git-{operation}-{uuid.uuid4().hex}.txt"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(text[:2_000_000], encoding="utf-8")
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id=capability_id, source_path=receipt,
            title=f"Git {operation} receipt", source_label="XV12 Git capability",
            conversation_id=str(arguments.get("conversation_id") or active_conversation_id() or "creator"),
            artifact_type="git_receipt", relevant_text=text[:60_000], actions=["view", "download", "copy"],
            metadata={"workspace_id": workspace_id, "operation": operation, "exit_code": result.returncode},
        )
        return {"status": "success" if result.returncode == 0 else "execution_error", "executed": True,
                "exit_code": result.returncode, "summary": text[-8000:], "artifact": artifact}

    def status(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        result = self._run(workspace_id, user["id"], ["status", "--short", "--branch"])
        return {"status": "success" if result.returncode == 0 else "execution_error", "workspace_id": workspace_id,
                "clean": result.returncode == 0 and not any(line and not line.startswith("##") for line in result.stdout.splitlines()),
                "summary": result.stdout[-12000:], "exit_code": result.returncode}

    def diff(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        argv = ["diff", "--no-ext-diff", "--stat"] if bool(arguments.get("stat_only")) else ["diff", "--no-ext-diff", "--"]
        result = self._run(workspace_id, user["id"], argv)
        return {"status": "success" if result.returncode == 0 else "execution_error", "workspace_id": workspace_id,
                "diff": result.stdout[:60_000], "truncated": len(result.stdout) > 60_000, "exit_code": result.returncode}

    def commit(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        add = self._run(workspace_id, user["id"], ["add", "--all"])
        if add.returncode != 0:
            return self._receipt("git.commit", workspace_id, user, arguments, add, "commit")
        result = self._run(workspace_id, user["id"], ["commit", "-m", str(arguments["message"])[:200]])
        return self._receipt("git.commit", workspace_id, user, arguments, result, "commit")

    def pull(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        result = self._run(workspace_id, user["id"], ["pull", "--ff-only"], timeout=300)
        return self._receipt("git.pull", workspace_id, user, arguments, result, "pull")

    def push(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        workspace_id = str(arguments["workspace_id"])
        upstream = self._run(workspace_id, user["id"], ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
        if upstream.returncode == 0:
            result = self._run(workspace_id, user["id"], ["push"], timeout=300)
        else:
            branch = self._run(workspace_id, user["id"], ["branch", "--show-current"])
            remotes = self._run(workspace_id, user["id"], ["remote"])
            if branch.returncode != 0 or not branch.stdout.strip() or "origin" not in remotes.stdout.split():
                result = self._run(workspace_id, user["id"], ["push"], timeout=300)
            else:
                result = self._run(workspace_id, user["id"], ["push", "--set-upstream", "origin", branch.stdout.strip()], timeout=300)
        return self._receipt("git.push", workspace_id, user, arguments, result, "push")


class MediaService:
    """Provider-neutral media facade with a real credential-free local design/video provider."""

    DESIGN_CUES = ("logo", "icon", "poster", "vector", "diagram", "infographic", "badge", "wordmark", "typography", "brand mark", "flat design")

    def __init__(self, store: CreatorStore, jobs: JobManager, artifacts: ArtifactStore, settings: Any) -> None:
        self.store, self.jobs, self.artifacts = store, jobs, artifacts
        self.media_root = (store.path.parent / "media").resolve()
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.comfyui = ComfyUIProvider(ComfyUIConfig.from_settings(settings))
        self.ffmpeg = shutil.which("ffmpeg")
        if not self.ffmpeg:
            candidates = list(Path.home().glob("AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"))
            self.ffmpeg = str(candidates[0]) if candidates else None
        browser_candidates = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"), Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")]
        self.browser = next((str(path) for path in browser_candidates if path.is_file()), None)

    def _conversation(self, arguments: dict[str, Any]) -> str:
        return str(arguments.get("conversation_id") or active_conversation_id() or "creator")

    def _user_dir(self, user_id: str) -> Path:
        path = self.media_root / _digest(user_id)[:20]
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _comfy_user_dir(self, user_id: str) -> Path:
        path = self.comfyui.config.output_path / _digest(user_id)[:20]
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def select_image_provider(cls, prompt: str, requested: str = "auto") -> tuple[str, str]:
        requested = str(requested or "auto").casefold()
        if requested == "design":
            return "xoduz-local-design", "explicit_design_provider"
        if requested == "comfyui":
            return "comfyui-photorealistic", "explicit_comfyui_provider"
        text = prompt.casefold()
        if any(cue in text for cue in cls.DESIGN_CUES):
            return "xoduz-local-design", "design_request"
        return "comfyui-photorealistic", "realistic_scene_default"

    def image_status(self, _arguments: dict[str, Any], _user: dict[str, Any]) -> dict[str, Any]:
        comfy = self.comfyui.status()
        return {"status": "success", "default_realistic_provider": "comfyui-photorealistic",
                "design_provider": {"provider": "xoduz-local-design", "status": "healthy"},
                "comfyui": comfy, "realistic_fallback_to_design": False}

    @staticmethod
    def _svg(prompt: str, width: int, height: int, source_data: str = "") -> str:
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        colors = [f"#{digest[index:index+6]}" for index in (0, 6, 12, 18)]
        words = [html.escape(word) for word in re.findall(r"[A-Za-z0-9'&-]+", prompt)[:18]]
        title = " ".join(words[:7]) or "Untitled creation"
        subtitle = " ".join(words[7:18])
        source = f'<image href="{source_data}" width="100%" height="100%" preserveAspectRatio="xMidYMid slice" opacity=".72"/>' if source_data else ""
        return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{colors[0]}"/><stop offset=".55" stop-color="{colors[1]}"/><stop offset="1" stop-color="{colors[2]}"/></linearGradient><filter id="blur"><feGaussianBlur stdDeviation="55"/></filter></defs>
<rect width="100%" height="100%" fill="url(#g)"/>{source}
<circle cx="{int(width*.78)}" cy="{int(height*.22)}" r="{int(min(width,height)*.25)}" fill="{colors[3]}" opacity=".48" filter="url(#blur)"/>
<path d="M0 {int(height*.78)} Q {int(width*.35)} {int(height*.56)} {int(width*.65)} {int(height*.83)} T {width} {int(height*.68)} V {height} H0Z" fill="#050712" opacity=".62"/>
<rect x="{int(width*.07)}" y="{int(height*.12)}" width="8" height="{int(height*.31)}" rx="4" fill="#fff" opacity=".85"/>
<text x="{int(width*.11)}" y="{int(height*.24)}" fill="#fff" font-family="Segoe UI,Arial" font-size="{max(28,int(width*.055))}" font-weight="700">{title}</text>
<text x="{int(width*.11)}" y="{int(height*.33)}" fill="#fff" opacity=".78" font-family="Segoe UI,Arial" font-size="{max(16,int(width*.022))}">{subtitle}</text>
<text x="{int(width*.07)}" y="{int(height*.92)}" fill="#fff" opacity=".58" font-family="Segoe UI,Arial" font-size="{max(13,int(width*.015))}" letter-spacing="3">CREATED WITH XODUZ</text>
</svg>'''

    def generate_image(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments["prompt"])
        provider, selection_reason = self.select_image_provider(prompt, str(arguments.get("provider") or "auto"))
        if provider == "comfyui-photorealistic":
            status = self.comfyui.status()
            if not status["healthy"]:
                return {"status": "unavailable", "provider": provider, "selected_because": selection_reason,
                        "message": "Photorealistic image generation is unavailable because the configured ComfyUI provider is not healthy. No design-poster fallback was used.",
                        "provider_status": status, "fallback_used": False}
            try:
                path, metadata = self.comfyui.generate(
                    prompt, self._comfy_user_dir(user["id"]), width=arguments.get("width"), height=arguments.get("height"),
                )
            except Exception as error:
                return {"status": "execution_error", "provider": provider, "selected_because": selection_reason,
                        "message": "ComfyUI failed before a valid image artifact was produced.",
                        "error": type(error).__name__, "fallback_used": False}
            artifact = self.artifacts.register_file(
                user_id=user["id"], capability_id="media.image.generate", source_path=path,
                title=str(arguments.get("title") or "Generated image"), source_label="XV12 ComfyUI photorealistic provider",
                conversation_id=self._conversation(arguments), artifact_type="image", actions=["view", "download"],
                metadata={**metadata, "selected_because": selection_reason, "fallback_used": False},
            )
            return {"status": "success", "provider": provider, "selected_because": selection_reason,
                    "actual_generation": True, "fallback_used": False, "artifact": artifact}
        width, height = min(max(int(arguments.get("width") or 1280), 256), 2048), min(max(int(arguments.get("height") or 720), 256), 2048)
        path = self._user_dir(user["id"]) / f"image-{uuid.uuid4().hex}.svg"
        path.write_text(self._svg(prompt, width, height), encoding="utf-8")
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id="media.image.generate", source_path=path,
            title=str(arguments.get("title") or "Generated image"), source_label="XODUZ built-in design provider",
            conversation_id=self._conversation(arguments), artifact_type="image", actions=["view", "download"],
            metadata={"provider": "xoduz-local-design", "width": width, "height": height, "prompt_sha256": _digest(prompt), "actual_generation": True,
                      "selected_because": selection_reason, "fallback_used": False},
        )
        return {"status": "success", "provider": "xoduz-local-design", "selected_because": selection_reason,
                "actual_generation": True, "fallback_used": False, "artifact": artifact}

    def edit_image(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        source = self.artifacts.get_owned(str(arguments["source_artifact_id"]), user["id"])
        if not source or not str(source.get("mime_type") or "").startswith("image/"):
            return {"status": "no_result", "message": "Owned source image artifact not found."}
        source_metadata = json.loads(source.get("metadata_json") or "{}")
        requested_provider = str(arguments.get("provider") or "auto").casefold()
        if source_metadata.get("provider") == "comfyui-photorealistic" and requested_provider != "design":
            return {"status": "unavailable", "provider": "comfyui-photorealistic",
                    "message": "ComfyUI image-to-image editing is not configured in this release. The original image was preserved; no design fallback was applied.",
                    "source_artifact_id": source["id"], "fallback_used": False}
        source_path = Path(str(source["source_path"]))
        mime = str(source["mime_type"])
        encoded = base64.b64encode(source_path.read_bytes()).decode()
        data_uri = f"data:{mime};base64,{encoded}"
        prompt = str(arguments["prompt"])
        width = int(source_metadata.get("width") or 1280)
        height = int(source_metadata.get("height") or 720)
        path = self._user_dir(user["id"]) / f"image-edit-{uuid.uuid4().hex}.svg"
        path.write_text(self._svg(prompt, width, height, data_uri), encoding="utf-8")
        artifact = self.artifacts.register_file(
            user_id=user["id"], capability_id="media.image.edit", source_path=path,
            title=str(arguments.get("title") or "Edited image"), source_label="XODUZ built-in design provider",
            conversation_id=self._conversation(arguments), artifact_type="image", parent_artifact_id=str(source["id"]), actions=["view", "download"],
            metadata={"provider": "xoduz-local-design", "width": width, "height": height, "prompt_sha256": _digest(prompt), "actual_edit": True},
        )
        return {"status": "success", "provider": "xoduz-local-design", "actual_edit": True, "fallback_used": False, "artifact": artifact,
                "source_artifact_id": source["id"]}

    def _source_png(self, source: dict[str, Any], target: Path) -> None:
        source_path = Path(str(source["source_path"]))
        if str(source.get("mime_type")) == "image/png":
            shutil.copy2(source_path, target)
            return
        if not self.browser:
            raise RuntimeError("Chromium is required to render the source image for video generation.")
        with tempfile.TemporaryDirectory(prefix="xv12-media-") as profile:
            result = subprocess.run(
                [self.browser, "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
                 "--window-size=1280,720", f"--screenshot={target}", source_path.as_uri()],
                capture_output=True, text=True, timeout=60,
            )
        if result.returncode != 0 or not target.is_file():
            raise RuntimeError("Source image could not be rendered for video generation.")

    def generate_video(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        source_id = str(arguments.get("source_artifact_id") or "")
        source = self.artifacts.get_owned(source_id, user["id"]) if source_id else None
        if not source or not str(source.get("mime_type") or "").startswith("image/"):
            return {"status": "no_result", "message": "A user-owned source image artifact is required."}
        if not self.ffmpeg:
            return {"status": "unavailable", "message": "No local video provider is configured."}
        duration = min(max(int(arguments.get("duration_seconds") or 10), 1), 30)
        conversation = self._conversation(arguments)
        safe_inputs = {"source_artifact_id": source_id, "duration_seconds": duration, "prompt_sha256": _digest(str(arguments["prompt"]))}

        def worker(_job_id: str, progress: Callable[[int, str], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
            user_dir = self._user_dir(user["id"])
            png = user_dir / f"video-source-{uuid.uuid4().hex}.png"
            target = user_dir / f"video-{uuid.uuid4().hex}.mp4"
            progress(12, "Rendering source image")
            self._source_png(source, png)
            if cancelled():
                return {}
            progress(35, "Encoding cinematic video")
            frames = duration * 24
            command = [
                str(self.ffmpeg), "-y", "-loop", "1", "-i", str(png), "-vf",
                f"scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,zoompan=z='min(zoom+0.0007,1.08)':d={frames}:s=1280x720:fps=24,format=yuv420p",
                "-t", str(duration), "-r", "24", "-c:v", "libx264", "-preset", "veryfast", "-movflags", "+faststart", str(target),
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=duration * 15 + 90)
            png.unlink(missing_ok=True)
            if result.returncode != 0 or not target.is_file() or target.stat().st_size < 1000:
                raise RuntimeError("Local video provider failed to encode the requested video.")
            progress(88, "Registering playable video")
            artifact = self.artifacts.register_file(
                user_id=user["id"], capability_id="media.video.generate", source_path=target,
                title=str(arguments.get("title") or "Generated video"), source_label="XODUZ local FFmpeg video provider",
                conversation_id=conversation, artifact_type="video", parent_artifact_id=source_id,
                actions=["play", "download"], metadata={"provider": "xoduz-local-ffmpeg", "duration_seconds": duration,
                  "source_artifact_id": source_id, "prompt_sha256": _digest(str(arguments["prompt"])), "actual_generation": True, "playable": True},
            )
            return {"artifact": artifact, "provider": "xoduz-local-ffmpeg", "source_artifact_id": source_id, "actual_generation": True}

        job = self.jobs.submit(user["id"], conversation, "video.generate", "", safe_inputs, worker)
        return {"status": "success", "queued": True, "job": job, "source_artifact_id": source_id, "provider": "xoduz-local-ffmpeg"}


class CreatorPlatform:
    def __init__(self, data_root: Path, artifacts: ArtifactStore, settings: Any) -> None:
        self.store = CreatorStore(data_root / "creator.sqlite", data_root / "workspaces")
        self.store.initialize()
        self.jobs = JobManager(self.store)
        self.secrets = SecretsBroker(self.store)
        self.workspaces = WorkspaceService(self.store, artifacts)
        self.sandbox = SandboxService(self.store, artifacts)
        self.previews = PreviewService(self.store, artifacts)
        self.preview_reconciliation = self.previews.reconcile()
        self.browser = BrowserService(self.store, artifacts)
        self.git = GitService(self.store, artifacts)
        self.media = MediaService(self.store, self.jobs, artifacts, settings)
        self.artifacts = artifacts
        self.builder_execution: BuilderExecutionService | None = None

    def configure_builder_execution(
        self, model_provider: Callable[[], Any], registry: Any, gateway: Any,
    ) -> None:
        self.builder_execution = BuilderExecutionService(
            store=self.store, jobs=self.jobs, workspaces=self.workspaces, previews=self.previews,
            artifacts=self.artifacts, model_provider=model_provider, registry=registry, gateway=gateway,
        )

    def execute_builder_session(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if not self.builder_execution:
            return {"status": "unavailable", "message": "Builder execution service is not configured."}
        return self.builder_execution.execute(arguments, user)

    def update_task_state(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        if not self.builder_execution:
            return {"status": "unavailable", "message": "Builder execution service is not configured."}
        session_id = str(arguments.get("session_id") or "")
        if not session_id:
            return {"status": "invalid_arguments", "message": "A Builder session ID is required."}
        patch = {key: value for key, value in arguments.items() if key != "session_id"}
        return self.builder_execution.task_state.update(session_id, user, patch)

    def register(self, gateway: Any) -> None:
        handlers = {
            "job.status": self.job_status, "job.cancel": self.job_cancel,
            "secrets.reference.configure": self.secrets.configure, "secrets.reference.status": self.secrets.status,
            "builder.workspace.create": self.workspaces.create, "builder.workspace.open": self.workspaces.open,
            "builder.session.execute": self.execute_builder_session,
            "builder.task_state.update": self.update_task_state,
            "builder.workspace.inspect": self.workspaces.inspect, "builder.files.read": self.workspaces.read,
            "builder.files.patch": self.workspaces.patch, "builder.files.batch": self.workspaces.batch,
            "builder.code.search": self.workspaces.code_search, "builder.code.map": self.workspaces.code_map,
            "builder.project.archive": self.workspaces.archive, "builder.sandbox.exec": self.sandbox.execute,
            "builder.preview.start": self.previews.start, "builder.preview.status": self.previews.status,
            "builder.preview.stop": self.previews.stop, "browser.preview.inspect": self.browser.inspect,
            "browser.preview.screenshot": self.browser.screenshot, "git.status": self.git.status,
            "git.diff": self.git.diff, "git.commit": self.git.commit, "git.pull": self.git.pull, "git.push": self.git.push,
            "media.image.status": self.media.image_status, "media.image.generate": self.media.generate_image, "media.image.edit": self.media.edit_image,
            "media.video.generate": self.media.generate_video,
        }
        for capability_id, handler in handlers.items():
            gateway.register(capability_id, handler)

    def job_status(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        item = self.store.job(str(arguments["job_id"]), user["id"])
        return {"status": "success", "job": CreatorStore.job_public(item)} if item else {"status": "no_result", "message": "Job not found."}

    def job_cancel(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        item = self.store.cancel_job(str(arguments["job_id"]), user["id"])
        return {"status": "success", "job": CreatorStore.job_public(item)} if item else {"status": "no_result", "message": "Job not found."}

    def health(self) -> dict[str, Any]:
        image = self.media.comfyui.status()
        return {
            "status": "available", "job_manager": "available", "sandbox": "available" if SandboxService.available() else "unavailable",
            "browser": "available" if self.browser.browser else "unavailable",
            "builder_execution": "available" if self.builder_execution else "unavailable",
            "builder_limits": {"soft_operations": 20, "hard_operations": 32, "repair_cycles": 6, "browser_cycles": 6},
            "image_provider": "comfyui-photorealistic" if image["healthy"] else "unavailable",
            "image_provider_status": image, "design_provider": "xoduz-local-design",
            "realistic_fallback_to_design": False,
            "video_provider": "xoduz-local-ffmpeg" if self.media.ffmpeg else "unavailable",
            "secret_values_exposed": False,
        }


def create_creator_router(platform: CreatorPlatform) -> APIRouter:
    router = APIRouter(prefix="/api/creator", tags=["creator"])

    @router.get("/jobs/{job_id}")
    def job_status(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        item = platform.store.job(job_id, user["id"])
        if not item:
            raise HTTPException(status_code=404, detail="Job not found")
        return CreatorStore.job_public(item)

    @router.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        item = platform.store.cancel_job(job_id, user["id"])
        if not item:
            raise HTTPException(status_code=404, detail="Job not found")
        return CreatorStore.job_public(item)

    @router.get("/builder-sessions/{session_id}")
    def builder_session_status(session_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        item = platform.store.builder_session(session_id, user["id"])
        if not item:
            raise HTTPException(status_code=404, detail="Builder session not found")
        return CreatorStore.builder_session_public(item)

    def render_preview(preview: dict[str, Any] | None, resource_path: str, request: Request) -> Response:
        if not preview or preview["state"] != "running":
            raise HTTPException(status_code=404, detail="Preview not found")
        if "\\" in resource_path or any(part in {"..", "."} for part in resource_path.split("/") if part):
            raise HTTPException(status_code=400, detail="Invalid preview path")
        upstream = f"http://127.0.0.1:{int(preview['port'])}/{resource_path.lstrip('/')}"
        if request.url.query:
            upstream += f"?{request.url.query}"
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                response = client.get(upstream)
            parsed = urlparse(str(response.url))
            if parsed.hostname != "127.0.0.1" or parsed.port != int(preview["port"]):
                raise HTTPException(status_code=502, detail="Managed preview redirect was rejected")
            if 300 <= response.status_code < 400:
                raise HTTPException(status_code=502, detail="Managed preview redirects are not allowed")
        except httpx.HTTPError as error:
            raise HTTPException(status_code=502, detail="Managed preview is unavailable") from error
        headers = {
            "Content-Type": response.headers.get("content-type", "application/octet-stream"),
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self' data: blob:; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'",
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=response.content, status_code=response.status_code, headers=headers)

    @router.get("/previews/{preview_id}/token/{access_token}/{resource_path:path}")
    def preview_token_proxy(preview_id: str, access_token: str, resource_path: str, request: Request) -> Response:
        preview = platform.store.preview_by_token(preview_id, access_token)
        return render_preview(preview, resource_path, request)

    @router.get("/previews/{preview_id}/{resource_path:path}")
    def preview_proxy(
        preview_id: str, resource_path: str, request: Request,
        user: dict[str, Any] = Depends(current_user),
    ) -> Response:
        return render_preview(platform.store.preview(preview_id, user["id"]), resource_path, request)

    return router
