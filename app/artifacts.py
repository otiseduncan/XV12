from __future__ import annotations

import contextvars
import json
import mimetypes
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .auth import current_user
from .registry import CapabilityDenied, CapabilityNotFound


_conversation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("xv12_artifact_conversation", default=None)
_user_message: contextvars.ContextVar[str] = contextvars.ContextVar("xv12_artifact_user_message", default="")


def active_conversation_id() -> str | None:
    return _conversation_id.get()


def active_user_message() -> str:
    return _user_message.get()


@contextmanager
def artifact_conversation(conversation_id: str | None) -> Iterator[None]:
    token = _conversation_id.set(conversation_id)
    try:
        yield
    finally:
        _conversation_id.reset(token)


class ConversationContextMiddleware:
    """Keep conversation identity available through the complete streaming ASGI call."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path") or "")
        parts = path.strip("/").split("/")
        conversation_id = parts[2] if len(parts) >= 4 and parts[:2] == ["api", "conversations"] else None
        message_token = _user_message.set("")
        try:
            if conversation_id and scope.get("method") == "POST" and parts[-1:] == ["stream"]:
                chunks: list[bytes] = []
                while True:
                    request_message = await receive()
                    chunks.append(bytes(request_message.get("body") or b""))
                    if not request_message.get("more_body"):
                        break
                body = b"".join(chunks)
                try:
                    payload = json.loads(body.decode("utf-8"))
                    _user_message.set(str(payload.get("message") or ""))
                except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                    pass
                replayed = False

                async def replay_receive() -> Any:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                effective_receive = replay_receive
            else:
                effective_receive = receive
            with artifact_conversation(conversation_id):
                await self.app(scope, effective_receive, send)
        finally:
            _user_message.reset(message_token)


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ArtifactStore:
    """User-scoped references to original files; binaries never enter chat or model context."""

    def __init__(self, path: Path, allowed_roots: list[Path]) -> None:
        self.path = path.resolve()
        self.allowed_roots = [root.resolve() for root in allowed_roots]

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    relevant_text TEXT NOT NULL DEFAULT '',
                    page_number INTEGER,
                    section TEXT,
                    actions_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS artifacts_user_conversation
                  ON artifacts(user_id,conversation_id,updated_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS artifacts_source_page
                  ON artifacts(user_id,conversation_id,capability_id,source_path,COALESCE(page_number,-1));
                CREATE TABLE IF NOT EXISTS artifact_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR REPLACE INTO artifact_meta(key,value) VALUES('schema_version','1');
                """
            )

    def _safe_path(self, source_path: Path) -> Path:
        path = source_path.resolve()
        if not any(path == root or path.is_relative_to(root) for root in self.allowed_roots):
            raise ValueError("Artifact source is outside configured authorized roots.")
        if not path.is_file():
            raise ValueError("Artifact source file is unavailable.")
        return path

    @staticmethod
    def _kind(mime_type: str) -> str:
        if mime_type == "application/pdf" or mime_type.startswith("text/") or "document" in mime_type:
            return "document"
        if mime_type.startswith("image/"):
            return "image"
        return "file"

    def register_file(
        self,
        *,
        user_id: str,
        capability_id: str,
        source_path: Path,
        title: str | None = None,
        source_label: str,
        conversation_id: str | None = None,
        page: int | None = None,
        section: str | None = None,
        relevant_text: str = "",
        actions: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation = conversation_id or active_conversation_id()
        if not conversation:
            raise ValueError("Artifact registration requires an active conversation.")
        path = self._safe_path(source_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        enabled_actions = actions or (["view", "download", "print", "copy"] if mime_type == "application/pdf" else ["view", "download", "copy"])
        now = utcnow()
        with self.connect() as db:
            existing = db.execute(
                """SELECT id FROM artifacts WHERE user_id=? AND conversation_id=? AND capability_id=?
                   AND source_path=? AND COALESCE(page_number,-1)=COALESCE(?,-1)""",
                (user_id, conversation, capability_id, str(path), page),
            ).fetchone()
            artifact_id = str(existing["id"]) if existing else str(uuid.uuid4())
            values = (
                artifact_id, user_id, conversation, capability_id, self._kind(mime_type),
                (title or path.name)[:240], mime_type, source_label[:120], str(path), relevant_text[:12000],
                page, (section or "")[:500] or None, json.dumps(enabled_actions),
                json.dumps({"size_bytes": path.stat().st_size, **(metadata or {})}), now, now,
            )
            db.execute(
                """INSERT INTO artifacts(id,user_id,conversation_id,capability_id,artifact_type,title,mime_type,
                   source_label,source_path,relevant_text,page_number,section,actions_json,metadata_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title,relevant_text=excluded.relevant_text,
                   section=excluded.section,actions_json=excluded.actions_json,metadata_json=excluded.metadata_json,
                   updated_at=excluded.updated_at""",
                values,
            )
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self.public(dict(row))

    @staticmethod
    def public(item: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(item["id"])
        stored_metadata = json.loads(item.get("metadata_json") or "{}")
        metadata = {
            key: value for key, value in stored_metadata.items()
            if not any(private in str(key).casefold() for private in ("path", "root", "cache", "internal"))
        }
        actions = json.loads(item.get("actions_json") or "[]")
        page = item.get("page_number")
        reference = f"/api/artifacts/{artifact_id}/content"
        return {
            "id": artifact_id,
            "type": item["artifact_type"],
            "title": item["title"],
            "mime_type": item["mime_type"],
            "source": item["source_label"],
            "reference": reference,
            "preview": {"url": reference, "page": page},
            "downloadable": "download" in actions,
            "printable": "print" in actions,
            "copyable": "copy" in actions,
            "metadata": {"page": page, "section": item.get("section"), **metadata},
        }

    def get_owned(self, artifact_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id)).fetchone()
        return dict(row) if row else None

    def recent_records(self, user_id: str, conversation_id: str, title: str = "", limit: int = 3) -> list[dict[str, Any]]:
        query = "SELECT * FROM artifacts WHERE user_id=? AND conversation_id=?"
        params: list[Any] = [user_id, conversation_id]
        if title.strip():
            query += " AND lower(title) LIKE lower(?)"
            params.append(f"%{title.strip()}%")
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 10))
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def recent(self, user_id: str, conversation_id: str, title: str = "", limit: int = 3) -> list[dict[str, Any]]:
        return [self.public(item) for item in self.recent_records(user_id, conversation_id, title, limit)]


def create_artifact_router(store: ArtifactStore) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])

    def authorized_item(artifact_id: str, request: Request, user: dict[str, Any]) -> dict[str, Any]:
        item = store.get_owned(artifact_id, user["id"])
        if not item:
            raise HTTPException(status_code=404, detail="Artifact not found")
        try:
            decision = request.app.state.registry.authorize(str(item["capability_id"]), user)
        except (CapabilityDenied, CapabilityNotFound, KeyError) as error:
            raise HTTPException(status_code=403, detail="Artifact source is not authorized") from error
        if not decision.allowed:
            raise HTTPException(status_code=403, detail="Artifact source is not authorized")
        return item

    @router.get("/{artifact_id}")
    def metadata(artifact_id: str, request: Request, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return store.public(authorized_item(artifact_id, request, user))

    @router.get("/{artifact_id}/content")
    def content(
        artifact_id: str,
        request: Request,
        download: bool = Query(default=False),
        user: dict[str, Any] = Depends(current_user),
    ) -> FileResponse:
        item = authorized_item(artifact_id, request, user)
        try:
            path = store._safe_path(Path(str(item["source_path"])))
        except ValueError as error:
            raise HTTPException(status_code=410, detail="Artifact source is no longer available") from error
        disposition = "attachment" if download else "inline"
        return FileResponse(
            path,
            media_type=str(item["mime_type"]),
            filename=path.name,
            content_disposition_type=disposition,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/{artifact_id}/text", response_class=PlainTextResponse)
    def relevant_text(artifact_id: str, request: Request, user: dict[str, Any] = Depends(current_user)) -> str:
        item = authorized_item(artifact_id, request, user)
        if "copy" not in json.loads(item.get("actions_json") or "[]"):
            raise HTTPException(status_code=404, detail="Copyable text is unavailable")
        return str(item.get("relevant_text") or "")

    return router
