from __future__ import annotations

import contextvars
import hashlib
import json
import mimetypes
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pypdf import PdfReader, PdfWriter

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
        except Exception:
            connection.rollback()
            raise
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
                    source_artifact_id TEXT NOT NULL DEFAULT '',
                    source_title TEXT NOT NULL DEFAULT '',
                    requested_scope TEXT NOT NULL DEFAULT '',
                    scope_kind TEXT NOT NULL DEFAULT 'full',
                    page_start INTEGER,
                    page_end INTEGER,
                    section_title TEXT,
                    subsection_title TEXT,
                    section_page_start INTEGER,
                    section_page_end INTEGER,
                    display_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS artifacts_user_conversation
                  ON artifacts(user_id,conversation_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS artifact_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                """
            )
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(artifacts)")}
            additions = {
                "source_artifact_id": "TEXT NOT NULL DEFAULT ''", "source_title": "TEXT NOT NULL DEFAULT ''",
                "requested_scope": "TEXT NOT NULL DEFAULT ''", "scope_kind": "TEXT NOT NULL DEFAULT 'full'",
                "page_start": "INTEGER", "page_end": "INTEGER", "section_title": "TEXT",
                "subsection_title": "TEXT", "section_page_start": "INTEGER", "section_page_end": "INTEGER",
                "display_key": "TEXT NOT NULL DEFAULT ''",
                "parent_artifact_id": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE artifacts ADD COLUMN {name} {declaration}")
            db.execute("UPDATE artifacts SET page_start=page_number WHERE page_start IS NULL AND page_number IS NOT NULL")
            db.execute("UPDATE artifacts SET page_end=page_number WHERE page_end IS NULL AND page_number IS NOT NULL")
            db.execute("UPDATE artifacts SET source_title=title WHERE source_title='' OR source_title IS NULL")
            rows = db.execute("SELECT id,source_path,page_start,page_end,section,scope_kind FROM artifacts").fetchall()
            for row in rows:
                source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(row["source_path"]).casefold()))
                scope_kind = "page" if row["page_start"] is not None else str(row["scope_kind"] or "full")
                key = self._display_key(source_id, row["page_start"], row["page_end"], str(row["section"] or ""), scope_kind)
                db.execute(
                    "UPDATE artifacts SET source_artifact_id=?,scope_kind=?,display_key=? WHERE id=?",
                    (source_id, scope_kind, key, row["id"]),
                )
            db.execute("DROP INDEX IF EXISTS artifacts_source_page")
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS artifacts_scope_identity ON artifacts(
                   user_id,conversation_id,capability_id,source_path,scope_kind,
                   COALESCE(page_start,-1),COALESCE(page_end,-1),COALESCE(section_title,''))"""
            )
            db.execute("INSERT OR REPLACE INTO artifact_meta(key,value) VALUES('schema_version','3')")

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
        if mime_type.startswith("video/"):
            return "video"
        return "file"

    @staticmethod
    def _display_key(source_id: str, page_start: Any, page_end: Any, section_title: str, scope_kind: str) -> str:
        identity = f"{source_id}|{scope_kind}|{page_start or ''}|{page_end or ''}|{section_title.casefold()}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _pdf_text(path: Path, page_start: int, page_end: int) -> str:
        reader = PdfReader(str(path), strict=False)
        start, end = max(page_start, 1), min(page_end, len(reader.pages))
        return "\n\n".join((reader.pages[index - 1].extract_text() or "") for index in range(start, end + 1))[:60000]

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
        source_title: str | None = None,
        requested_scope: str = "",
        scope_kind: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        section_title: str | None = None,
        subsection_title: str | None = None,
        section_page_start: int | None = None,
        section_page_end: int | None = None,
        relevant_text: str = "",
        actions: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        artifact_type: str | None = None,
        parent_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = conversation_id or active_conversation_id()
        if not conversation:
            raise ValueError("Artifact registration requires an active conversation.")
        path = self._safe_path(source_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start = page_start if page_start is not None else page
        end = page_end if page_end is not None else start
        resolved_scope_kind = scope_kind or ("page" if start is not None and start == end else "section" if start is not None else "full")
        resolved_section = section_title or section
        source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(path).casefold()))
        display_key = self._display_key(source_id, start, end, str(resolved_section or ""), resolved_scope_kind)
        enabled_actions = actions or (["view", "download", "print", "copy", "full_document"] if mime_type == "application/pdf" and resolved_scope_kind != "full" else ["view", "download", "print"] if mime_type == "application/pdf" else ["view", "download", "copy"])
        now = utcnow()
        artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user_id}|{conversation}|{capability_id}|{display_key}"))
        artifact_metadata = {"size_bytes": path.stat().st_size, **(metadata or {})}
        if mime_type == "application/pdf" and "source_page_count" not in artifact_metadata:
            artifact_metadata["source_page_count"] = len(PdfReader(str(path), strict=False).pages)
        payload = {
            "id": artifact_id, "user_id": user_id, "conversation_id": conversation,
            "capability_id": capability_id, "artifact_type": artifact_type or self._kind(mime_type),
            "title": (title or path.name)[:240], "mime_type": mime_type, "source_label": source_label[:120],
            "source_path": str(path), "relevant_text": relevant_text[:60000], "page_number": start,
            "section": (resolved_section or "")[:500] or None, "actions_json": json.dumps(enabled_actions),
            "metadata_json": json.dumps(artifact_metadata),
            "source_artifact_id": source_id, "source_title": (source_title or path.name)[:240],
            "requested_scope": requested_scope[:1000], "scope_kind": resolved_scope_kind,
            "page_start": start, "page_end": end, "section_title": (resolved_section or "")[:500] or None,
            "subsection_title": (subsection_title or "")[:500] or None,
            "section_page_start": section_page_start, "section_page_end": section_page_end,
            "display_key": display_key, "created_at": now, "updated_at": now,
            "parent_artifact_id": (parent_artifact_id or "")[:100],
        }
        with self.connect() as db:
            db.execute(
                """INSERT INTO artifacts(id,user_id,conversation_id,capability_id,artifact_type,title,mime_type,
                   source_label,source_path,relevant_text,page_number,section,actions_json,metadata_json,
                   source_artifact_id,source_title,requested_scope,scope_kind,page_start,page_end,section_title,
                   subsection_title,section_page_start,section_page_end,display_key,created_at,updated_at,parent_artifact_id)
                   VALUES(:id,:user_id,:conversation_id,:capability_id,:artifact_type,:title,:mime_type,
                   :source_label,:source_path,:relevant_text,:page_number,:section,:actions_json,:metadata_json,
                   :source_artifact_id,:source_title,:requested_scope,:scope_kind,:page_start,:page_end,:section_title,
                   :subsection_title,:section_page_start,:section_page_end,:display_key,:created_at,:updated_at,:parent_artifact_id)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title,relevant_text=excluded.relevant_text,
                   section=excluded.section,actions_json=excluded.actions_json,metadata_json=excluded.metadata_json,
                   requested_scope=excluded.requested_scope,scope_kind=excluded.scope_kind,page_start=excluded.page_start,
                   page_end=excluded.page_end,section_title=excluded.section_title,subsection_title=excluded.subsection_title,
                   section_page_start=excluded.section_page_start,section_page_end=excluded.section_page_end,
                   updated_at=excluded.updated_at""",
                payload,
            )
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return self.public(dict(row))

    def derive(
        self,
        item: dict[str, Any],
        *,
        scope_kind: str,
        page_start: int | None = None,
        page_end: int | None = None,
        requested_scope: str = "",
        title: str | None = None,
    ) -> dict[str, Any]:
        path = self._safe_path(Path(str(item["source_path"])))
        section_title = str(item.get("section_title") or item.get("section") or "") or None
        subsection = str(item.get("subsection_title") or "") or None
        if scope_kind == "section":
            page_start = int(item.get("section_page_start") or item.get("page_start") or 1)
            page_end = int(item.get("section_page_end") or item.get("page_end") or page_start)
            subsection = None
        elif scope_kind == "full":
            page_start = page_end = None
            section_title = subsection = None
        elif scope_kind == "page":
            page_end = page_start
        if path.suffix.casefold() == ".pdf" and page_start and page_end:
            total_pages = len(PdfReader(str(path), strict=False).pages)
            if page_start < 1 or page_end < page_start or page_end > total_pages:
                raise ValueError("Requested artifact page is outside the source document.")
        relevant = self._pdf_text(path, page_start, page_end) if path.suffix.casefold() == ".pdf" and page_start and page_end else ""
        if scope_kind == "page":
            derived_title = f"{item.get('source_title') or path.stem} — Page {page_start}"
        elif scope_kind == "full":
            derived_title = str(item.get("source_title") or path.name)
        else:
            derived_title = title or section_title or str(item.get("title") or path.name)
        return self.register_file(
            user_id=str(item["user_id"]), conversation_id=str(item["conversation_id"]),
            capability_id=str(item["capability_id"]), source_path=path, title=derived_title,
            source_title=str(item.get("source_title") or path.name), source_label=str(item["source_label"]),
            requested_scope=requested_scope, scope_kind=scope_kind, page_start=page_start, page_end=page_end,
            section_title=section_title, subsection_title=subsection,
            section_page_start=item.get("section_page_start"), section_page_end=item.get("section_page_end"),
            relevant_text=relevant, metadata=json.loads(item.get("metadata_json") or "{}"),
        )

    def public(self, item: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(item["id"])
        stored_metadata = json.loads(item.get("metadata_json") or "{}")
        metadata = {
            key: value for key, value in stored_metadata.items()
            if not any(private in str(key).casefold() for private in ("path", "root", "cache", "internal"))
        }
        actions = json.loads(item.get("actions_json") or "[]")
        page_start = item.get("page_start") if item.get("page_start") is not None else item.get("page_number")
        page_end = item.get("page_end") if item.get("page_end") is not None else page_start
        scope_kind = str(item.get("scope_kind") or ("page" if page_start else "full"))
        scoped_pdf = item.get("mime_type") == "application/pdf" and scope_kind != "full" and page_start is not None
        reference = f"/api/artifacts/{artifact_id}/content"
        full_reference = f"/api/artifacts/{artifact_id}/full" if scoped_pdf else None
        return {
            "id": artifact_id,
            "type": item["artifact_type"],
            "title": item["title"],
            "mime_type": item["mime_type"],
            "source": item["source_label"],
            "source_artifact_id": item.get("source_artifact_id"),
            "source_title": item.get("source_title") or item["title"],
            "source_capability": item.get("capability_id"),
            "parent_artifact_id": item.get("parent_artifact_id") or None,
            "requested_scope": item.get("requested_scope") or "",
            "page_start": page_start, "page_end": page_end,
            "section_title": item.get("section_title") or item.get("section"),
            "subsection_title": item.get("subsection_title"),
            "reference": reference,
            "preview": {"url": reference, "page": 1 if scoped_pdf else page_start},
            "downloadable": "download" in actions,
            "printable": "print" in actions,
            "copyable": "copy" in actions,
            "full_document_reference": full_reference,
            "display_key": item.get("display_key") or self._display_key(str(item.get("source_artifact_id") or ""), page_start, page_end, str(item.get("section_title") or ""), scope_kind),
            "parent": {"id": item.get("source_artifact_id"), "reference": full_reference} if full_reference else None,
            "metadata": {
                "page": page_start if page_start == page_end else None, "page_start": page_start, "page_end": page_end,
                "section": item.get("section_title") or item.get("section"), "subsection": item.get("subsection_title"),
                "scope_kind": scope_kind, "requested_scope": item.get("requested_scope") or "",
                "section_page_start": item.get("section_page_start"), "section_page_end": item.get("section_page_end"),
                **metadata,
            },
        }

    def materialize(self, item: dict[str, Any], *, full: bool = False) -> Path:
        source = self._safe_path(Path(str(item["source_path"])))
        start, end = item.get("page_start"), item.get("page_end")
        if full or str(item.get("mime_type")) != "application/pdf" or str(item.get("scope_kind") or "full") == "full" or start is None:
            return source
        reader = PdfReader(str(source), strict=False)
        start, end = int(start), int(end or start)
        if start < 1 or end < start or end > len(reader.pages):
            raise ValueError("Artifact page scope is outside the source document.")
        cache = self.path.parent / "artifact_slices"
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / f"{item['id']}-{source.stat().st_mtime_ns}-{start}-{end}.pdf"
        if target.is_file():
            return target
        writer = PdfWriter()
        for index in range(start - 1, end):
            writer.add_page(reader.pages[index])
        writer.add_metadata({"/Title": str(item.get("title") or source.stem), "/Subject": f"Original pages {start}-{end}"})
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as output:
            writer.write(output)
        temporary.replace(target)
        return target

    @staticmethod
    def delivery_name(item: dict[str, Any], *, full: bool = False) -> str:
        source = Path(str(item["source_path"]))
        if full or str(item.get("scope_kind") or "full") == "full":
            return source.name
        start, end = item.get("page_start"), item.get("page_end")
        suffix = f"page-{start}" if start == end else f"pages-{start}-{end}"
        stem = re.sub(r"[^A-Za-z0-9._ -]+", "", source.stem).strip() or "document"
        return f"{stem}-{suffix}.pdf"

    def get_owned(self, artifact_id: str, user_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id)).fetchone()
        return dict(row) if row else None

    def recent_records(self, user_id: str, conversation_id: str, title: str = "", limit: int = 3) -> list[dict[str, Any]]:
        query = "SELECT * FROM artifacts WHERE user_id=? AND conversation_id=?"
        params: list[Any] = [user_id, conversation_id]
        if title.strip():
            query += " AND (lower(title) LIKE lower(?) OR lower(source_title) LIKE lower(?) OR lower(COALESCE(section_title,'')) LIKE lower(?) OR lower(COALESCE(subsection_title,'')) LIKE lower(?) OR lower(requested_scope) LIKE lower(?))"
            params.extend([f"%{title.strip()}%"] * 5)
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
            path = store.materialize(item)
        except ValueError as error:
            raise HTTPException(status_code=410, detail="Artifact source is no longer available") from error
        disposition = "attachment" if download else "inline"
        return FileResponse(
            path,
            media_type=str(item["mime_type"]),
            filename=store.delivery_name(item),
            content_disposition_type=disposition,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/{artifact_id}/full")
    def full_document(
        artifact_id: str,
        request: Request,
        download: bool = Query(default=False),
        user: dict[str, Any] = Depends(current_user),
    ) -> FileResponse:
        item = authorized_item(artifact_id, request, user)
        try:
            path = store.materialize(item, full=True)
        except ValueError as error:
            raise HTTPException(status_code=410, detail="Artifact source is no longer available") from error
        return FileResponse(
            path, media_type=str(item["mime_type"]), filename=store.delivery_name(item, full=True),
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("/{artifact_id}/text", response_class=PlainTextResponse)
    def relevant_text(artifact_id: str, request: Request, user: dict[str, Any] = Depends(current_user)) -> str:
        item = authorized_item(artifact_id, request, user)
        if "copy" not in json.loads(item.get("actions_json") or "[]"):
            raise HTTPException(status_code=404, detail="Copyable text is unavailable")
        return str(item.get("relevant_text") or "")

    return router
