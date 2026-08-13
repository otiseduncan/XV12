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
from pypdf import PdfReader

from .auth import create_auth_router, current_user
from .assistant import AssistantOrchestrator, build_evidence_snapshot
from .artifacts import ArtifactStore, ConversationContextMiddleware, active_conversation_id, active_user_message, create_artifact_router
from .capabilities.adas_si import AdasSICapability
from .capabilities.calibration_iq import CalibrationIQCapability
from .capabilities.engineering import RepoInspectionService
from .capabilities.files import LocalFilesCapability
from .config import Settings
from .context import ContextAssembler
from .creator import CreatorPlatform, create_creator_router
from .data_tools import adas_coverage, adas_search, calibration_iq_health, calibration_iq_read, start_calibration_iq
from .database import utcnow
from .enrollment import EnrollmentCapabilityRegistry, EnrolledUserAccessMiddleware, EnrollmentMiddleware, EnrollmentStore
from .model import LlamaModel
from .model_compat import ToolCallCompatibilityModel
from .permissions import CapabilityPermissionStore, create_permission_router
from .registry import CapabilityDenied, CapabilityGateway, CapabilityNotFound
from .web_tools import current_search
from .remote_access import create_remote_access_router


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=100)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


class CapabilityCall(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=100)


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    reference: str | None = Field(default=None, max_length=500)
    description: str = Field(default="", max_length=2000)


class PreferredNameUpdate(BaseModel):
    preferred_name: str = Field(min_length=1, max_length=80)


class VoiceSettingsUpdate(BaseModel):
    voice_name: str | None = Field(default=None, min_length=1, max_length=120)
    voice_volume: int | None = Field(default=None, ge=0, le=100)
    voice_muted: bool | None = None


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


# Bounded, content-aware attachment ingestion. Text-like source files get a direct inline
# excerpt; PDFs get a short excerpt from their first pages plus a real registered artifact so
# existing page-ranged artifact tooling (adas/local-files pattern) covers the rest of the
# document. Never an indiscriminate full-file dump into the prompt.
ATTACHMENT_TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".css", ".html", ".htm", ".log", ".yml", ".yaml", ".csv", ".ini", ".toml",
}
ATTACHMENT_INGEST_FILE_LIMIT = 3
ATTACHMENT_EXCERPT_CHAR_LIMIT = 3000
ATTACHMENT_TOTAL_EXCERPT_CHAR_LIMIT = 8000
ATTACHMENT_PDF_EXCERPT_PAGES = 3


def _resolve_attachment_path(settings: Settings, storage_path: str) -> Path | None:
    """storage_path is written root-relative when attachments_path sits under settings.root
    (the normal production layout), or attachments_path-relative with a leading 'attachments'
    marker segment otherwise (e.g. an isolated test environment) -- see the two branches in
    upload_attachment. Resolve whichever form actually produced the file, and always require
    the result to still be inside the owned attachments root."""
    owned_root = settings.attachments_path.resolve()
    candidate = (settings.root / storage_path).resolve()
    if candidate.is_relative_to(owned_root) and candidate.is_file():
        return candidate
    relative = Path(storage_path)
    if relative.parts and relative.parts[0] == "attachments":
        relative = Path(*relative.parts[1:]) if len(relative.parts) > 1 else Path()
    fallback = (settings.attachments_path / relative).resolve()
    if fallback.is_relative_to(owned_root) and fallback.is_file():
        return fallback
    return None


def _ingest_attachment_excerpt(path: Path, content_type: str) -> str:
    suffix = path.suffix.casefold()
    try:
        if suffix == ".pdf" or content_type == "application/pdf":
            reader = PdfReader(str(path), strict=False)
            pages = min(len(reader.pages), ATTACHMENT_PDF_EXCERPT_PAGES)
            text = "\n\n".join((reader.pages[index].extract_text() or "") for index in range(pages))
            return text[:ATTACHMENT_EXCERPT_CHAR_LIMIT]
        if suffix in ATTACHMENT_TEXT_SUFFIXES or content_type.startswith("text/"):
            data = path.read_bytes()[:200_000]
            if b"\x00" in data[:4096]:
                return ""
            return data.decode("utf-8", errors="replace")[:ATTACHMENT_EXCERPT_CHAR_LIMIT]
    except Exception:
        return ""
    return ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = EnrollmentStore(settings.database_path, settings.owner_google_sub, settings)
    store.initialize()
    settings.attachments_path.mkdir(parents=True, exist_ok=True)
    capability_data = settings.root / "data" / "capabilities" if settings.root in settings.database_path.resolve().parents else settings.database_path.parent / "capabilities"
    permission_store = CapabilityPermissionStore(capability_data / "permissions.sqlite", settings.database_path)
    permission_store.initialize()
    store.permission_store = permission_store
    registry = EnrollmentCapabilityRegistry(settings.root / "config" / "capabilities.v1.json", permission_store)
    artifact_store = ArtifactStore(
        capability_data / "artifacts.sqlite",
        [settings.root, Path(r"X:\ADAS SI"), settings.calibration_iq_project_path, capability_data / "creator"],
    )
    artifact_store.initialize()
    creator_platform = CreatorPlatform(capability_data / "creator", artifact_store, settings)

    _WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"']+")
    _ENGINEERING_TOOL_FLOOR = {"system.health.read", "artifact.recent.read", "project.list"}
    _BUILD_REQUEST_PHRASES = (
        "build me", "build a website", "build a web app", "build an app", "build a landing page",
        "create a website", "create a web app", "create an app", "modify the website", "modify my app",
        "modify the app", "update the website", "update my app",
    )

    def narrow_tools_for_strong_signal(items: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
        """Deterministic relevance narrowing: reduce tool noise when the request carries one
        unambiguous strong signal (an explicit filesystem path, a named service by name, or an
        explicit build/modify request). Simple keyword/pattern matching only -- never a brittle
        intent classifier -- and only narrows the offered tool set; the model still owns
        response wording and whether to call anything at all. Ambiguous or signal-free
        requests are left untouched so ordinary conversation is never over-narrowed."""
        folded = message.casefold()
        has_path = bool(_WINDOWS_PATH_RE.search(message))
        has_calibration_iq = "calibration iq" in folded
        has_adas = "adas" in folded
        has_build_request = any(phrase in folded for phrase in _BUILD_REQUEST_PHRASES)
        signals = (has_path, has_calibration_iq, has_adas, has_build_request)
        if sum(1 for signal in signals if signal) != 1:
            return items
        if has_path:
            keep_families = {"engineering", "files"}
        elif has_calibration_iq:
            keep_families = {"calibration_iq"}
        elif has_adas:
            keep_families = {"adas_si"}
        else:
            keep_families = {"builder"}
        return [item for item in items if item["id"] in _ENGINEERING_TOOL_FLOOR or item.get("family") in keep_families]

    def filter_model_tools(items: list[dict[str, Any]], user: dict[str, Any]) -> list[dict[str, Any]]:
        conversation_id = active_conversation_id()
        raw_message = active_user_message()
        message = raw_message.casefold().strip()
        referential_action = any(action in message for action in ("display", "open", "view", "print", "download", "copy", "show"))
        referential_object = any(reference in message for reference in ("the document", "that document", "this document", "whole document", "entire document", "full document", "the file", "that file", "the pdf", "that pdf", "this pdf", "that page", "this page", "the section", "that section", "this section", "whole section", "entire section", "next page", "previous page", "print it", "download it", "copy it", "open it", "display it")) or bool(re.search(r"\bpage\s+\d+\b|\b(?:whole|entire|complete|full)\b.*\bsection\b", message))
        asks_for_new_source = any(phrase in message for phrase in ("another document", "different document", "new document", "search for", "find a document"))
        has_recent = bool(conversation_id and artifact_store.recent_records(user["id"], conversation_id, "", 1))
        if referential_action and referential_object and has_recent and not asks_for_new_source:
            retrieval_ids = {"adas.si.search", "adas.knowledge.search", "web.current.search", "files.local.read"}
            items = [item for item in items if item["id"] not in retrieval_ids]
        return narrow_tools_for_strong_signal(items, raw_message)

    registry.model_tool_filter = filter_model_tools
    gateway = CapabilityGateway(registry)
    files_capability = LocalFilesCapability(
        [settings.root, Path(r"X:\ADAS SI"), settings.calibration_iq_project_path],
        capability_data / "files",
        artifact_store,
        attachments_root=settings.attachments_path,
    )
    engineering_service = RepoInspectionService([settings.root, Path(r"X:\ADAS SI"), settings.calibration_iq_project_path])
    adas_si_capability = AdasSICapability(Path(r"X:\ADAS SI"), capability_data / "adas_si" / "index.sqlite", artifact_store)
    calibration_iq_capability = CalibrationIQCapability(settings)
    model = ToolCallCompatibilityModel(LlamaModel(settings))
    context = ContextAssembler(store, settings.model_context_tokens, registry)
    turn_logger = _configure_logging(settings)
    gateway.audit_logger = turn_logger

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
    app.state.permission_store = permission_store
    app.state.artifact_store = artifact_store
    app.state.creator_platform = creator_platform
    app.state.turn_logger = turn_logger
    app.include_router(create_auth_router(settings))
    app.include_router(create_permission_router(permission_store))
    app.include_router(create_artifact_router(artifact_store))
    app.include_router(create_creator_router(creator_platform))
    app.include_router(create_remote_access_router(settings))
    app.add_middleware(ConversationContextMiddleware)
    app.add_middleware(EnrollmentMiddleware)
    app.add_middleware(EnrolledUserAccessMiddleware)

    async def health_document() -> dict[str, Any]:
        model_health = await app.state.model.health()
        runtime_config = json.loads((settings.root / "config" / "runtime.json").read_text(encoding="utf-8"))
        executable = settings.root / runtime_config["model"]["executable"]
        model_path = settings.root / runtime_config["model"]["path"]
        return {
            "ok": bool(model_health.get("reachable") and model_health.get("alias_ok")),
            "application": {"name": "XODUZ XV12", "status": "healthy"},
            "database": {"status": "healthy", "schema": store.schema_version(), "path_owned": settings.root in settings.database_path.parents},
            "model": {
                **model_health,
                "expected_alias": settings.model_alias,
                "context_tokens": settings.model_context_tokens,
                "executable_owned": settings.root in executable.resolve().parents,
                "model_owned": settings.root in model_path.resolve().parents,
            },
            "auth": {"mode": settings.auth_mode, "admin_count": store.admin_count()},
            "registry": {"version": registry.version, "count": len(registry.capabilities)},
            "services": {
                "adas": {"status": "available" if (settings.adas_database_path or settings.root / "data/knowledge/adas_knowledge.sqlite").exists() else "offline"},
                "calibration_iq": await calibration_iq_health(settings),
                "web": {"status": "available", "providers": ["Bing News RSS", "DuckDuckGo HTML"]},
                "creator": creator_platform.health(),
            },
        }

    gateway.register("system.health.read", lambda _: health_document())
    gateway.register("admin.capabilities.inspect", lambda _: {"registry_version": registry.version, "capabilities": list(registry.capabilities)})
    gateway.register("web.current.search", lambda arguments: current_search(arguments, settings.web_timeout_seconds))

    def recent_artifacts(arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        conversation_id = active_conversation_id()
        if not conversation_id:
            return {"status": "no_result", "artifacts": [], "message": "No active conversation artifact context is available."}
        requested_title = str(arguments.get("title") or "")
        records = artifact_store.recent_records(
            user["id"], conversation_id, requested_title, int(arguments.get("limit") or 3)
        )
        if not records and requested_title:
            records = artifact_store.recent_records(
                user["id"], conversation_id, "", int(arguments.get("limit") or 3)
            )
        authorized_records = []
        for item in records:
            try:
                decision = registry.authorize(str(item["capability_id"]), user)
            except (KeyError, CapabilityDenied, CapabilityNotFound):
                continue
            if decision.allowed:
                authorized_records.append(item)
        artifacts = []
        if authorized_records:
            current = authorized_records[0]
            message = active_user_message().casefold()
            requested_scope = "current"
            if any(phrase in message for phrase in ("whole document", "entire document", "full document", "whole manual", "entire manual")):
                requested_scope = "full"
            elif "section" in message and any(word in message for word in ("whole", "entire", "complete", "full")):
                requested_scope = "section"
            page_match = re.search(r"\bpage\s+(\d+)\b", message)
            requested_page = int(page_match.group(1)) if page_match else None
            if requested_page:
                requested_scope = "page"
            elif "next page" in message:
                requested_scope, requested_page = "page", int(current.get("page_end") or current.get("page_start") or 0) + 1
            elif "previous page" in message:
                requested_scope, requested_page = "page", max(1, int(current.get("page_start") or 2) - 1)
            try:
                if requested_scope == "current":
                    artifacts.append(artifact_store.public(current))
                else:
                    artifacts.append(
                        artifact_store.derive(
                            current, scope_kind=requested_scope, page_start=requested_page,
                            requested_scope=active_user_message(),
                        )
                    )
            except ValueError:
                artifacts = []
        return {
            "status": "success" if artifacts else "no_result",
            "artifacts": artifacts,
            "requested_action": arguments.get("action") or "display",
            "reused_existing_reference": bool(artifacts),
        }

    gateway.register("artifact.recent.read", recent_artifacts)
    gateway.register("adas.coverage.read", lambda arguments: adas_coverage(settings, arguments))
    gateway.register("adas.knowledge.search", lambda arguments: adas_search(settings, arguments))
    gateway.register("files.local.read", files_capability.read)
    gateway.register("files.local.write", files_capability.write)
    gateway.register("files.local.modify", files_capability.modify)
    gateway.register("files.local.batch_read", files_capability.batch_read)
    gateway.register("engineering.repo.map", engineering_service.map)
    gateway.register("engineering.code.search", engineering_service.search)
    gateway.register("engineering.files.read", engineering_service.read)
    gateway.register("engineering.files.batch_read", engineering_service.batch_read)
    gateway.register("engineering.git.status", engineering_service.git_status)
    gateway.register("engineering.git.diff", engineering_service.git_diff)
    gateway.register("engineering.tests.inspect", engineering_service.tests_inspect)
    gateway.register("adas.si.inventory.read", adas_si_capability.inventory)
    gateway.register("adas.si.search", adas_si_capability.search)
    gateway.register("adas.si.record.write", adas_si_capability.write)
    gateway.register("adas.si.record.modify", adas_si_capability.modify)
    gateway.register("calibration_iq.repair_orders.read", calibration_iq_capability.read)
    gateway.register("calibration_iq.repair_orders.write", calibration_iq_capability.write)
    gateway.register("calibration_iq.repair_orders.modify", calibration_iq_capability.modify)
    gateway.register("project.list", lambda _arguments, user: {"status": "verified", "projects": store.list_projects(user["id"])})

    def _bind_active_project(user_id: str, project_id: str) -> dict[str, Any] | None:
        """Activate a project both as the user's global last-selected project (preserves the
        standalone Projects UI and its header chip) and, when called from within an open
        conversation, as that conversation's own bound project -- so a different conversation
        activating a different project cannot bleed into this one."""
        activated = store.activate_project(user_id, project_id)
        conversation_id = active_conversation_id()
        if activated and conversation_id:
            store.activate_project_for_conversation(user_id, conversation_id, project_id)
        return activated

    def register_project_capability(arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        project = store.create_project(
            user["id"],
            str(arguments.get("name") or ""),
            arguments.get("reference"),
            str(arguments.get("description") or ""),
        )
        activated = _bind_active_project(user["id"], project["id"])
        return {"status": "registered_and_activated", "project": activated}

    gateway.register(
        "project.register",
        register_project_capability,
    )
    gateway.register(
        "project.activate",
        lambda arguments, user: {
            "status": "activated" if (project := _bind_active_project(user["id"], str(arguments.get("project_id") or ""))) else "not_found",
            "project": project,
        },
    )

    def _detach_active_project(user_id: str) -> bool:
        changed = store.deactivate_project(user_id)
        conversation_id = active_conversation_id()
        if conversation_id:
            changed = store.deactivate_project_for_conversation(user_id, conversation_id) or changed
        return changed

    gateway.register("project.detach", lambda _arguments, user: {"status": "detached", "changed": _detach_active_project(user["id"])})
    gateway.register("service.calibration_iq.start", lambda arguments: start_calibration_iq(settings, arguments))
    gateway.register(
        "settings.voice.read",
        lambda _arguments, user: {"status": "verified", "settings": store.get_voice_settings(user["id"])},
    )
    gateway.register(
        "settings.voice.update",
        lambda arguments, user: {"status": "updated", "settings": store.set_voice_settings(user["id"], arguments)},
    )
    creator_platform.configure_builder_execution(lambda: app.state.model, registry, gateway)
    creator_platform.register(gateway)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
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

    @app.get("/api/settings/voice")
    def get_voice_settings(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return store.get_voice_settings(user["id"])

    @app.patch("/api/settings/voice")
    def update_voice_settings(payload: VoiceSettingsUpdate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        changes = {
            key: value
            for key, value in {
                "voice_name": payload.voice_name,
                "voice_volume": payload.voice_volume,
                "voice_muted": payload.voice_muted,
            }.items()
            if value is not None
        }
        return store.set_voice_settings(user["id"], changes)

    @app.post("/api/conversations", status_code=201)
    def create_conversation(payload: ConversationCreate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return store.create_conversation(user["id"], payload.title)

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        item = store.get_conversation(user["id"], conversation_id)
        if not item:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return item

    @app.patch("/api/conversations/{conversation_id}")
    def rename_conversation(conversation_id: str, payload: ConversationUpdate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        item = store.rename_conversation(user["id"], conversation_id, payload.title)
        if not item:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return item

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: str, user: dict[str, Any] = Depends(current_user)) -> Response:
        if not store.delete_conversation(user["id"], conversation_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return Response(status_code=204)

    @app.get("/api/projects")
    def list_projects(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
        return store.list_projects(user["id"])

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectCreate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        return store.create_project(user["id"], payload.name, payload.reference, payload.description)

    @app.post("/api/projects/{project_id}/activate")
    def activate_project(project_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        project = store.activate_project(user["id"], project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    @app.delete("/api/projects/active", status_code=204)
    def deactivate_project(user: dict[str, Any] = Depends(current_user)) -> Response:
        store.deactivate_project(user["id"])
        return Response(status_code=204)

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

    @app.delete("/api/attachments/{attachment_id}", status_code=204)
    def delete_attachment(attachment_id: str, user: dict[str, Any] = Depends(current_user)) -> Response:
        item = store.delete_attachment(user["id"], attachment_id)
        if not item:
            raise HTTPException(status_code=404, detail="Attachment not found")
        target = _resolve_attachment_path(settings, item["storage_path"])
        if target:
            target.unlink(missing_ok=True)
        return Response(status_code=204)

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
            lines = ["\n\nAttachments supplied:"] + [
                f"- {item['original_name']} ({item['content_type']}, {item['size_bytes']} bytes)" for item in attachments
            ]
            excerpt_budget = ATTACHMENT_TOTAL_EXCERPT_CHAR_LIMIT
            for item in attachments[:ATTACHMENT_INGEST_FILE_LIMIT]:
                if excerpt_budget <= 0:
                    break
                attachment_path = _resolve_attachment_path(settings, item["storage_path"])
                if not attachment_path:
                    continue
                excerpt = _ingest_attachment_excerpt(attachment_path, str(item["content_type"]))[:excerpt_budget]
                if not excerpt:
                    continue
                excerpt_budget -= len(excerpt)
                truncated = " (truncated)" if len(excerpt) >= ATTACHMENT_EXCERPT_CHAR_LIMIT else ""
                lines.append(f"\n--- {item['original_name']} excerpt{truncated} ---\n{excerpt}")
                if attachment_path.suffix.casefold() == ".pdf" or item["content_type"] == "application/pdf":
                    try:
                        artifact_store.register_file(
                            user_id=user["id"], capability_id="attachment.ingest", source_path=attachment_path,
                            title=item["original_name"], source_label="Uploaded attachment",
                            conversation_id=conversation_id, scope_kind="full",
                        )
                    except ValueError:
                        pass
            attachment_note = "\n".join(lines)
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
                orchestrator = AssistantOrchestrator(app.state.model, registry, gateway)
                cards: list[dict[str, Any]] = []
                terminal: dict[str, Any] = {"status": "complete", "stop_reason": "natural_completion", "telemetry": {}}
                async for event in orchestrator.stream(assembled.messages, user):
                    if await request.is_disconnected():
                        raise asyncio.CancelledError()
                    if event["type"] == "content":
                        text = str(event["text"])
                        if first_token_at is None:
                            first_token_at = utcnow()
                            store.update_trace(turn_id, first_token_at=first_token_at)
                        response_parts.append(text)
                        yield sse("delta", {"text": text})
                    elif event["type"] == "capability_start":
                        yield sse("capability", {"status": "running", "capability_id": event["capability_id"], "arguments": event["arguments"]})
                    elif event["type"] == "capability_result":
                        card = {key: event[key] for key in ("capability_id", "arguments", "result")}
                        cards.append(card)
                        yield sse("capability", {"status": "complete", **card})
                    elif event["type"] == "complete":
                        terminal = {key: event[key] for key in ("status", "stop_reason", "telemetry") if key in event}
                content = "".join(response_parts).strip()
                if not content:
                    raise RuntimeError("Model completed without response content")
                message_status = str(terminal.get("status") or "complete")
                telemetry = dict(terminal.get("telemetry") or {})
                metadata = {
                    "capability_cards": cards,
                    "stop_reason": terminal.get("stop_reason", "natural_completion"),
                    "final_synthesis_performed": telemetry.get("final_synthesis_performed", False),
                    **{key: telemetry[key] for key in ("model_rounds", "operation_count", "capability_count") if key in telemetry},
                }
                assistant = store.add_message(user["id"], conversation_id, "assistant", content, message_status, metadata)
                if message_status == "complete":
                    store.clear_evidence(user["id"], conversation_id)
                else:
                    evidence = build_evidence_snapshot(payload.message, cards, str(terminal.get("stop_reason") or ""), content)
                    store.save_evidence(user["id"], conversation_id, evidence)
                completed = utcnow()
                detail = {"characters": len(content), "attachments": len(attachments), "capability_calls": len(cards), "status": message_status, **telemetry}
                store.update_trace(turn_id, completed_at=completed, status=message_status, detail=detail)
                turn_logger.info(json.dumps({"event": "turn_complete", "turn_id": turn_id, "conversation_id": conversation_id, "user_id": user["id"], "first_token_at": first_token_at, **detail}))
                yield sse("done", {"message_id": assistant["id"], "status": message_status, "stop_reason": terminal.get("stop_reason", "natural_completion")})
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
    async def list_capabilities(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        capabilities = [dict(item) for item in registry.list_for(user)]
        calibration_health = await calibration_iq_health(settings)
        for item in capabilities:
            if item["id"].startswith("calibration_iq."):
                item["health"] = calibration_health["status"]
            elif item["id"].startswith("adas.si."):
                item["health"] = "available" if Path(r"X:\ADAS SI").is_dir() else "offline"
            elif item["id"].startswith("adas."):
                item["health"] = "available" if (settings.adas_database_path or settings.root / "data/knowledge/adas_knowledge.sqlite").exists() else "offline"
        return {"registry_version": registry.version, "capabilities": capabilities}

    @app.post("/api/capabilities/{capability_id}")
    async def execute_capability(capability_id: str, payload: CapabilityCall, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        try:
            result, decision = await gateway.execute(capability_id, user, payload.arguments)
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

    @app.patch("/api/admin/users/{user_id}/preferred-name")
    def update_preferred_name(user_id: str, payload: PreferredNameUpdate, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Administrator role required")
        updated = store.set_preferred_name(user_id, payload.preferred_name)
        if not updated:
            raise HTTPException(status_code=404, detail="User not found")
        return {"id": updated["id"], "conversational_name": updated.get("preferred_name") or "User", "role": updated["role"]}

    static_dir = settings.root / "app" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/assets", StaticFiles(directory=settings.root / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/service-worker.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            static_dir / "service-worker.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.exception_handler(Exception)
    async def unhandled_error(_: Request, error: Exception) -> JSONResponse:
        turn_logger.info(json.dumps({"event": "unhandled_error", "error": type(error).__name__}))
        return JSONResponse(status_code=500, content={"detail": "XV12 encountered an internal error"})

    return app


app = create_app()
