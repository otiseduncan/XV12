from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import create_auth_router, current_user
from .config import Settings
from .context import ContextAssembler
from .database import UserScopedStore, utcnow
from .model import LlamaModel
from .registry import CapabilityDenied, CapabilityGateway, CapabilityRegistry


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=100)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


class CapabilityCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


def sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _configure_logging(settings: Settings) -> logging.Logger:
    log_dir = settings.root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("xv12.turns")
    if not logger.handlers:
        handler = logging.FileHandler(log_dir / "turns.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _file_sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = UserScopedStore(settings.database_path, settings.owner_google_sub)
    store.initialize()
    settings.attachments_path.mkdir(parents=True, exist_ok=True)
    registry = CapabilityRegistry(settings.root / "config" / "capabilities.v1.json")
    gateway = CapabilityGateway(registry)
    model = LlamaModel(settings)
    context = ContextAssembler(store, settings.model_context_tokens)
    turn_logger = _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield

    app = FastAPI(title="XODUZ XV12", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.model = model
    app.state.context = context
    app.state.registry = registry
    app.state.gateway = gateway
    app.state.turn_logger = turn_logger
    app.include_router(create_auth_router(settings))

    async def health_document() -> dict[str, Any]:
        model_health = await app.state.model.health()
        runtime_config = json.loads((settings.root / "config" / "runtime.json").read_text(encoding="utf-8"))
        executable = settings.root / runtime_config["model"]["executable"]
        model_path = settings.root / runtime_config["model"]["path"]
        return {
            "ok": bool(model_health.get("reachable") and model_health.get("alias_ok")),
            "application": {"name": "XODUZ XV12", "status": "healthy"},
            "database": {"status": "healthy", "schema": "1", "path_owned": settings.root in settings.database_path.parents},
            "model": {
                **model_health,
                "expected_alias": settings.model_alias,
                "context_tokens": settings.model_context_tokens,
                "executable_owned": settings.root in executable.resolve().parents,
                "model_owned": settings.root in model_path.resolve().parents,
            },
            "auth": {"mode": settings.auth_mode, "admin_count": store.admin_count()},
            "registry": {"version": registry.version, "count": len(registry.capabilities)},
        }

    gateway.register("system.health.read", lambda _: {"status": "healthy"})
    gateway.register("admin.capabilities.inspect", lambda _: {"registry_version": registry.version, "capabilities": list(registry.capabilities)})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return await health_document()

    @app.get("/api/runtime/fingerprint")
    async def runtime_fingerprint(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        runtime_config = json.loads((settings.root / "config" / "runtime.json").read_text(encoding="utf-8"))
        executable = settings.root / runtime_config["model"]["executable"]
        model_path = settings.root / runtime_config["model"]["path"]
        result = {
            "alias": settings.model_alias,
            "context_tokens": settings.model_context_tokens,
            "model_path": str(model_path.relative_to(settings.root)),
            "runtime_path": str(executable.relative_to(settings.root)),
            "model_size": model_path.stat().st_size if model_path.exists() else None,
            "runtime_sha256": _file_sha256(executable) if executable.exists() else None,
        }
        if user["role"] == "admin" and model_path.exists():
            manifest_path = settings.root / "config" / "baseline-manifest.json"
            if manifest_path.exists():
                result["model_sha256"] = json.loads(manifest_path.read_text(encoding="utf-8")).get("model", {}).get("sha256")
        return result

    @app.get("/api/conversations")
    def list_conversations(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
        return store.list_conversations(user["id"])

    @app.post("/api/conversations", status_code=201)
    def create_conversation(payload: ConversationCreate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return store.create_conversation(user["id"], payload.title)

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        item = store.get_conversation(user["id"], conversation_id)
        if not item:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return item

    @app.post("/api/attachments", status_code=201)
    async def upload_attachment(
        file: UploadFile = File(...),
        conversation_id: str | None = Form(default=None),
        user: dict[str, Any] = Depends(current_user),
    ) -> dict[str, Any]:
        original_name = Path(file.filename or "attachment").name[:240]
        extension = Path(original_name).suffix[:16]
        user_dir = settings.attachments_path / user["id"]
        user_dir.mkdir(parents=True, exist_ok=True)
        target = user_dir / f"{uuid.uuid4()}{extension}"
        size = 0
        try:
            with target.open("xb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        raise HTTPException(status_code=413, detail="Attachment limit is 10 MB")
                    stream.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        try:
            storage_reference = (
                str(target.relative_to(settings.root))
                if target.is_relative_to(settings.root)
                else str(Path("attachments") / target.relative_to(settings.attachments_path))
            )
            item = store.add_attachment(
                user["id"], conversation_id, original_name, storage_reference, file.content_type or "application/octet-stream", size
            )
        except KeyError as error:
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {key: item[key] for key in ("id", "original_name", "content_type", "size_bytes", "created_at")}

    @app.post("/api/conversations/{conversation_id}/stream")
    async def stream_chat(
        conversation_id: str,
        payload: ChatRequest,
        request: Request,
        user: dict[str, Any] = Depends(current_user),
    ) -> StreamingResponse:
        if not store.get_conversation(user["id"], conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        try:
            attachments = store.attach_to_conversation(user["id"], conversation_id, payload.attachment_ids)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        attachment_note = ""
        if attachments:
            attachment_note = "\n\nAttachments supplied (metadata only in Baseline 1):\n" + "\n".join(
                f"- {item['original_name']} ({item['content_type']}, {item['size_bytes']} bytes)" for item in attachments
            )
        user_message = store.add_message(user["id"], conversation_id, "user", payload.message.strip() + attachment_note)
        active_subject = store.ensure_active_subject(user["id"], conversation_id, payload.message)
        turn_id = str(uuid.uuid4())

        async def generate():
            response_parts: list[str] = []
            first_token_at: str | None = None
            try:
                try:
                    await context.compact_if_needed(app.state.model, user, conversation_id)
                except Exception as compaction_error:
                    turn_logger.info(json.dumps({"event": "summary_failed", "turn_id": turn_id, "error": type(compaction_error).__name__}))
                assembled = context.assemble(user, conversation_id)
                store.create_trace(
                    {
                        "turn_id": turn_id,
                        "user_id": user["id"],
                        "conversation_id": conversation_id,
                        "context_tokens": assembled.estimated_tokens,
                        "context_sections": assembled.sections,
                        "active_subject": active_subject or assembled.active_subject,
                        "summary_used": assembled.summary_used,
                        "status": "streaming",
                    }
                )
                yield sse("meta", {"turn_id": turn_id, "message_id": user_message["id"], "context_tokens": assembled.estimated_tokens, "sections": assembled.sections})
                started = utcnow()
                store.update_trace(turn_id, model_started_at=started)
                async for text in app.state.model.stream(assembled.messages):
                    if await request.is_disconnected():
                        raise asyncio.CancelledError()
                    if first_token_at is None:
                        first_token_at = utcnow()
                        store.update_trace(turn_id, first_token_at=first_token_at)
                    response_parts.append(text)
                    yield sse("delta", {"text": text})
                content = "".join(response_parts).strip()
                if not content:
                    raise RuntimeError("Model completed without response content")
                assistant = store.add_message(user["id"], conversation_id, "assistant", content, "complete")
                completed = utcnow()
                detail = {"characters": len(content), "attachments": len(attachments)}
                store.update_trace(turn_id, completed_at=completed, status="complete", detail=detail)
                turn_logger.info(json.dumps({"event": "turn_complete", "turn_id": turn_id, "conversation_id": conversation_id, "user_id": user["id"], "first_token_at": first_token_at, **detail}))
                yield sse("done", {"message_id": assistant["id"], "status": "complete"})
            except asyncio.CancelledError:
                content = "".join(response_parts).strip()
                if content:
                    store.add_message(user["id"], conversation_id, "assistant", content, "interrupted")
                store.update_trace(turn_id, completed_at=utcnow(), status="interrupted", detail={"characters": len(content)})
                turn_logger.info(json.dumps({"event": "turn_interrupted", "turn_id": turn_id, "characters": len(content)}))
                raise
            except Exception as error:
                content = "".join(response_parts).strip()
                if content:
                    store.add_message(user["id"], conversation_id, "assistant", content, "failed")
                try:
                    store.update_trace(turn_id, completed_at=utcnow(), status="failed", detail={"error": type(error).__name__, "characters": len(content)})
                except Exception:
                    pass
                turn_logger.info(json.dumps({"event": "turn_failed", "turn_id": turn_id, "error": type(error).__name__}))
                yield sse("error", {"message": "X could not complete that response. Check XV12 model health and logs.", "turn_id": turn_id})

        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/capabilities")
    def list_capabilities(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return {"registry_version": registry.version, "capabilities": registry.list_for(user)}

    @app.post("/api/capabilities/{capability_id}")
    def execute_capability(capability_id: str, payload: CapabilityCall, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        try:
            result, decision = gateway.execute(capability_id, user, payload.arguments)
        except CapabilityDenied as error:
            raise HTTPException(status_code=403, detail="Capability is not authorized for this user") from error
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return {"result": result, "authorization": asdict(decision)}

    @app.post("/api/admin/users/{user_id}/revoke", status_code=204)
    def revoke_user_sessions(user_id: str, user: dict[str, Any] = Depends(current_user)) -> Response:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator role required")
        store.revoke_user_sessions(user_id)
        return Response(status_code=204)

    static_dir = settings.root / "app" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/assets", StaticFiles(directory=settings.root / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, error: Exception) -> JSONResponse:
        turn_logger.info(json.dumps({"event": "unhandled_error", "error": type(error).__name__}))
        return JSONResponse(status_code=500, content={"detail": "XV12 encountered an internal error"})

    return app


app = create_app()
