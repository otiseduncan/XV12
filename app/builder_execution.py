from __future__ import annotations

import asyncio
import copy
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .model_compat import ToolCallCompatibilityModel


BUILDER_TOOL_IDS = (
    "builder.workspace.inspect",
    "builder.code.search",
    "builder.code.map",
    "builder.files.read",
    "builder.files.patch",
    "builder.files.batch",
    "builder.task_state.update",
    "builder.sandbox.exec",
    "builder.preview.start",
    "builder.preview.status",
    "builder.preview.stop",
    "browser.preview.inspect",
    "browser.preview.screenshot",
    "builder.project.archive",
    "git.status",
    "git.diff",
)

SOFT_OPERATION_LIMIT = 20
HARD_OPERATION_LIMIT = 32
MODEL_ROUND_LIMIT = 20
REPAIR_CYCLE_LIMIT = 6
BROWSER_CYCLE_LIMIT = 6
WALL_TIME_LIMIT_SECONDS = 1200
CONTEXT_CHARACTER_LIMIT = 120_000
BUILDER_MODEL_MAX_TOKENS = 4096
TURN_JOB_REUSE_SECONDS = 90
ASSET_SOURCE_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".json", ".py"}
DIFF_CONTEXT_FILE_LIMIT = 12
DIFF_CONTEXT_FILE_CHAR_LIMIT = 4000
DIFF_CONTEXT_TOTAL_CHAR_LIMIT = 24_000
QUALITY_CRITIQUE_ISSUE_LIMIT = 10
STYLE_TELEMETRY_CHAR_LIMIT = 9000

# --- TaskState ---------------------------------------------------------------------------
# TaskState is a durable engineering record answering what is being built, what has been
# learned, what remains, and why. It is distinct from BuilderEvidence below, which answers
# only what objective proof has been obtained. Never store hidden reasoning here -- only
# conclusions, decisions, requirements, and execution state.
TASK_STATE_LIST_FIELDS = (
    "requirements", "constraints", "plan", "completed", "open_items",
    "changed_files", "current_failures", "latest_critique",
)
TASK_STATE_ARCHITECTURE_LIST_FIELDS = ("entry_points", "components", "interfaces", "important_files")
TASK_STATE_LIST_LIMIT = 24
TASK_STATE_ITEM_CHAR_LIMIT = 400
TASK_STATE_STRING_FIELD_LIMIT = 1200
TASK_STATE_SUMMARY_CHAR_LIMIT = 3200

# Two-class change-scope classification. Deliberately simple -- do not add magnitude levels
# without benchmark evidence that two classes are insufficient.
SUBSTANTIAL_CHANGE_KEYWORDS = (
    "redesign", "restructure", "refactor", "overhaul", "rebuild", "rearchitect",
    "re-architect", "new feature", "add a feature", "major", "revamp", "replace the",
    "cross-module", "cross module", "rewrite", "cross-file", "multi-file", "multiple files",
    "new layout", "responsive redesign", "visual refresh", "design refresh", "significant feature",
    "overall look", "entire", "whole site", "whole app", "from scratch",
)

# Context compaction: how many of the most recent raw exchanges stay verbatim once
# engineering-aware compaction kicks in.
BUILDER_RAW_TAIL_MESSAGES = 12


def default_task_state(goal: str = "", change_scope: str = "") -> dict[str, Any]:
    """Return a fresh TaskState. See the module-level TaskState comment above BUILDER_TOOL_IDS."""
    return {
        "goal": str(goal)[:TASK_STATE_STRING_FIELD_LIMIT],
        "change_scope": str(change_scope)[:80],
        "requirements": [],
        "constraints": [],
        "architecture": {"entry_points": [], "components": [], "interfaces": [], "important_files": []},
        "plan": [],
        "completed": [],
        "open_items": [],
        "changed_files": [],
        "current_failures": [],
        "latest_validation": {},
        "latest_critique": [],
        "next_action": "",
    }


def _bounded_task_state_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:TASK_STATE_ITEM_CHAR_LIMIT] for item in value if str(item).strip()][:TASK_STATE_LIST_LIMIT]


def parse_task_state(raw: Any, goal: str = "", change_scope: str = "") -> dict[str, Any]:
    """Deserialize a persisted TaskState, falling back to a fresh default state on any
    corruption rather than raising. TaskState must never block the engineering loop."""
    state = default_task_state(goal, change_scope)
    data: Any = raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return state
    if not isinstance(data, dict):
        return state
    return merge_task_state(state, data)


def merge_task_state(state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial TaskState update. Keys omitted from patch are preserved unchanged;
    keys present in patch fully replace the prior value, after bounds/type validation."""
    merged = copy.deepcopy(state)
    if not isinstance(patch, dict):
        return merged
    if "goal" in patch and str(patch["goal"] or "").strip():
        merged["goal"] = str(patch["goal"])[:TASK_STATE_STRING_FIELD_LIMIT]
    if "change_scope" in patch and str(patch["change_scope"] or "").strip():
        merged["change_scope"] = str(patch["change_scope"])[:80]
    for list_field in TASK_STATE_LIST_FIELDS:
        if list_field in patch:
            merged[list_field] = _bounded_task_state_list(patch[list_field])
    if "architecture" in patch and isinstance(patch["architecture"], dict):
        architecture = dict(merged.get("architecture") or {})
        for arch_field in TASK_STATE_ARCHITECTURE_LIST_FIELDS:
            if arch_field in patch["architecture"]:
                architecture[arch_field] = _bounded_task_state_list(patch["architecture"][arch_field])
        merged["architecture"] = architecture
    if "latest_validation" in patch and isinstance(patch["latest_validation"], dict):
        merged["latest_validation"] = {
            str(key)[:80]: (value if isinstance(value, (bool, int, float)) else str(value)[:500])
            for key, value in list(patch["latest_validation"].items())[:20]
        }
    if "next_action" in patch and str(patch["next_action"] or "").strip():
        merged["next_action"] = str(patch["next_action"])[:TASK_STATE_STRING_FIELD_LIMIT]
    return merged


def summarize_task_state(state: dict[str, Any]) -> str:
    """Render a compact, model-facing TaskState summary for injection every round. Contains
    conclusions, decisions, and execution state only -- never hidden reasoning."""
    architecture = state.get("architecture") or {}
    lines = [
        f"Goal: {state.get('goal') or '(not yet recorded)'}",
        f"Change scope: {state.get('change_scope') or '(not yet classified)'}",
    ]

    def section(label: str, items: list[str]) -> None:
        if items:
            lines.append(f"{label}: " + " | ".join(items[:12]))

    section("Requirements", state.get("requirements") or [])
    section("Constraints", state.get("constraints") or [])
    section("Entry points", architecture.get("entry_points") or [])
    section("Components", architecture.get("components") or [])
    section("Important files", architecture.get("important_files") or [])
    section("Plan", state.get("plan") or [])
    section("Completed", state.get("completed") or [])
    section("Open items", state.get("open_items") or [])
    section("Changed files", state.get("changed_files") or [])
    section("Current failures", state.get("current_failures") or [])
    section("Latest critique", state.get("latest_critique") or [])
    validation = state.get("latest_validation") or {}
    if validation:
        lines.append("Latest validation: " + json.dumps(validation, ensure_ascii=False, default=str)[:600])
    if state.get("next_action"):
        lines.append(f"Next action: {state['next_action']}")
    text = "\n".join(lines)
    if len(text) > TASK_STATE_SUMMARY_CHAR_LIMIT:
        text = text[:TASK_STATE_SUMMARY_CHAR_LIMIT] + " …(truncated)"
    return text


def classify_change_scope(request: str, mode: str, has_existing_workspace: bool) -> str:
    """Two-class scope classification: targeted_change for a bounded fix/adjustment,
    substantial_change for anything requiring broad orientation and an explicit plan. A
    fresh build always gets full orientation since the workspace starts empty."""
    if mode == "build" and not has_existing_workspace:
        return "substantial_change"
    lowered = request.casefold()
    if any(keyword in lowered for keyword in SUBSTANTIAL_CHANGE_KEYWORDS):
        return "substantial_change"
    return "targeted_change"


class TaskStateService:
    """Owns TaskState persistence for Builder sessions, alongside BuilderEvidence."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def load(self, session: dict[str, Any]) -> dict[str, Any]:
        return parse_task_state(session.get("task_state_json"), goal=str(session.get("original_request") or ""))

    def save(self, session_id: str, state: dict[str, Any]) -> dict[str, Any]:
        self.store.update_builder_session(session_id, task_state_json=json.dumps(state, ensure_ascii=False))
        return state

    def update(self, session_id: str, user: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """Model-facing partial update via the builder.task_state.update tool. Scoped to the
        caller's own session the same way every other Builder capability is user-scoped."""
        session = self.store.builder_session(session_id, user["id"])
        if not session:
            return {"status": "no_result", "message": "Builder session not found."}
        merged = merge_task_state(self.load(session), patch)
        self.save(session_id, merged)
        return {"status": "success", "task_state": merged}


@dataclass(slots=True)
class BuilderEvidence:
    workspace_id: str
    preview_id: str = ""
    application_artifact: dict[str, Any] | None = None
    screenshot_artifact: dict[str, Any] | None = None
    archive_artifact: dict[str, Any] | None = None
    files_changed: bool = False
    sandbox_succeeded: bool = False
    browser_healthy: bool = False
    browser_failed: bool = False
    operation_count: int = 0
    model_rounds: int = 0
    repair_cycles: int = 0
    browser_cycles: int = 0
    latest_observations: list[dict[str, Any]] = field(default_factory=list)
    test_build_summary: str = ""
    browser_title: str = ""
    browser_body_text: str = ""
    requirements_review_cycles: int = 0
    quality_review_cycles: int = 0
    style_telemetry: str = ""
    staged_assets: list[dict[str, Any]] = field(default_factory=list)
    asset_usage: list[dict[str, Any]] = field(default_factory=list)

    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.files_changed:
            missing.append("write or patch the requested application files")
        if not self.sandbox_succeeded:
            missing.append("run a successful bounded test or build in the sandbox")
        if not self.preview_id or not self.application_artifact:
            missing.append("start or reuse a managed application preview")
        if not self.browser_healthy:
            missing.append("obtain a healthy Chromium validation with no runtime or network failures")
        return missing


class BuilderExecutionService:
    """Durable model-directed engineering loop isolated from ordinary chat rounds."""

    def __init__(
        self,
        *,
        store: Any,
        jobs: Any,
        workspaces: Any,
        previews: Any,
        artifacts: Any,
        model_provider: Callable[[], Any],
        registry: Any,
        gateway: Any,
    ) -> None:
        self.store = store
        self.jobs = jobs
        self.workspaces = workspaces
        self.previews = previews
        self.artifacts = artifacts
        self.model_provider = model_provider
        self.registry = registry
        self.gateway = gateway
        self.task_state = TaskStateService(store)
        self._turn_jobs: dict[tuple[str, str, str], tuple[str, float]] = {}

    def _reuse_job_response(self, job: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "success",
            "queued": str(job.get("state") or "") not in {"succeeded", "failed", "cancelled"},
            "reused_existing_job": True,
            "message": (
                "This conversation turn already has a Builder job. No second Builder job was created; "
                "the existing chat progress/result card is authoritative."
            ),
            "do_not_poll_in_this_turn": True,
            "active_job": self.store.job_public(job),
            "builder_session": self.store.builder_session_public(session),
            "workspace_id": str(session.get("workspace_id") or ""),
        }

    def _stage_conversation_assets(
        self, user_id: str, conversation_id: str, workspace_id: str,
    ) -> list[dict[str, Any]]:
        """Copy recent owned conversation images into the Builder workspace, most-recent first."""
        root = self.store.safe_path(workspace_id, user_id, ".", must_exist=True)
        target_root = root / "assets" / "xv12"
        staged: list[dict[str, Any]] = []
        try:
            records = self.artifacts.recent_records(user_id, conversation_id, limit=10)
        except Exception:
            return staged
        for record in records:
            if str(record.get("artifact_type") or "") != "image":
                continue
            if not str(record.get("mime_type") or "").startswith("image/"):
                continue
            try:
                source = self.artifacts.materialize(record)
            except Exception:
                continue
            suffix = source.suffix.casefold() or ".img"
            artifact_id = str(record.get("id") or "")
            relative = Path("assets") / "xv12" / f"artifact-{artifact_id[:12]}{suffix}"
            target = root / relative
            target_root.mkdir(parents=True, exist_ok=True)
            try:
                if not target.is_file() or target.stat().st_size != source.stat().st_size:
                    shutil.copy2(source, target)
            except OSError:
                continue
            staged.append({
                "artifact_id": artifact_id,
                "title": str(record.get("title") or source.name)[:240],
                "mime_type": str(record.get("mime_type") or ""),
                "path": relative.as_posix(),
                "most_recent": len(staged) == 0,
            })
            if len(staged) >= 4:
                break
        return staged

    def _scan_asset_usage(self, workspace_id: str, user_id: str, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return evidence that staged assets are referenced by actual application source, not merely copied."""
        if not assets:
            return []
        root = self.store.safe_path(workspace_id, user_id, ".", must_exist=True)
        candidates: list[tuple[str, str]] = []
        ignored = {".git", "node_modules", ".creator-deps", ".xv12-artifacts", "__pycache__", ".pytest_cache"}
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in ASSET_SOURCE_SUFFIXES:
                continue
            relative = path.relative_to(root)
            if any(part in ignored for part in relative.parts):
                continue
            lowered_parts = {part.casefold() for part in relative.parts}
            if "tests" in lowered_parts or relative.name.casefold().startswith("test_") or ".test." in relative.name.casefold():
                continue
            try:
                if path.stat().st_size > 750_000:
                    continue
                candidates.append((relative.as_posix(), path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        usage: list[dict[str, Any]] = []
        for asset in assets:
            asset_path = str(asset.get("path") or "")
            basename = Path(asset_path).name
            references = [name for name, text in candidates if asset_path in text or basename in text]
            usage.append({
                "artifact_id": asset.get("artifact_id"),
                "title": asset.get("title"),
                "path": asset_path,
                "referenced_by": references[:20],
                "referenced": bool(references),
            })
        return usage

    def execute(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        request = str(arguments.get("request") or "").strip()
        if not request:
            return {"status": "invalid_arguments", "message": "A Builder request is required."}
        conversation_id = str(arguments.get("conversation_id") or "").strip()
        from .artifacts import active_conversation_id, active_user_message

        if not conversation_id:
            conversation_id = str(active_conversation_id() or "")
        if not conversation_id:
            return {"status": "invalid_arguments", "message": "Builder execution requires an active conversation."}

        mode = str(arguments.get("mode") or "build").casefold()
        if mode not in {"build", "modify", "repair", "continue"}:
            return {"status": "invalid_arguments", "message": "Builder mode must be build, modify, repair, or continue."}
        parent = self.store.latest_builder_session(user["id"], conversation_id)
        workspace_id = str(arguments.get("workspace_id") or "")
        force_new_workspace = bool(arguments.get("new_workspace"))
        turn_message = str(active_user_message() or "").strip()
        turn_key = (str(user["id"]), conversation_id, turn_message) if turn_message else None

        # Calls made by the model in the same user turn share active_user_message. Reuse that turn's
        # first Builder job even if it completed before a later model-generated "status" call arrives.
        if turn_key and not force_new_workspace:
            now = time.monotonic()
            for key, (_, created) in list(self._turn_jobs.items()):
                if now - created > TURN_JOB_REUSE_SECONDS:
                    self._turn_jobs.pop(key, None)
            cached = self._turn_jobs.get(turn_key)
            if cached:
                cached_job = self.store.job(cached[0], user["id"])
                cached_session = self.store.latest_builder_session(user["id"], conversation_id)
                if cached_job and cached_session and str(cached_session.get("job_id") or "") == str(cached_job.get("id") or ""):
                    return self._reuse_job_response(cached_job, cached_session)

        # A Builder job owns the engineering lifecycle for one conversation. The chat card polls
        # that durable job directly; a model attempt to check status must not create a competitor.
        if parent and not force_new_workspace:
            active_job_id = str(parent.get("job_id") or "")
            active_job = self.store.job(active_job_id, user["id"]) if active_job_id else None
            if active_job and str(active_job.get("state") or "") in {"queued", "running", "cancelling"}:
                return self._reuse_job_response(active_job, parent)

        if workspace_id:
            if not self.store.workspace(workspace_id, user["id"]):
                return {"status": "no_result", "message": "The requested Builder workspace is unavailable."}
        elif parent and not force_new_workspace:
            if not self.store.workspace(str(parent["workspace_id"]), user["id"]):
                return {"status": "no_result", "message": "The prior Builder workspace is unavailable."}
            workspace_id = str(parent["workspace_id"])
            if mode == "build":
                mode = "modify"
        elif mode in {"modify", "repair", "continue"}:
            if not parent or not self.store.workspace(str(parent["workspace_id"]), user["id"]):
                return {"status": "no_result", "message": "No resumable Builder workspace exists in this conversation."}
            workspace_id = str(parent["workspace_id"])
        else:
            title = str(arguments.get("title") or "").strip() or " ".join(request.split()[:8])
            workspace_id = str(self.store.create_workspace(user["id"], title).get("id") or "")

        staged_assets = self._stage_conversation_assets(user["id"], conversation_id, workspace_id)
        session = self.store.create_builder_session(
            user_id=user["id"],
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            request=request,
            mode=mode,
            project_id=str(arguments.get("project_id") or ""),
            parent_session_id=str(parent.get("id") or "") if parent and str(parent.get("workspace_id")) == workspace_id else "",
        )
        session_id = str(session["id"])

        def worker(
            job_id: str,
            progress: Callable[[int, str], None],
            cancelled: Callable[[], bool],
        ) -> dict[str, Any]:
            self.store.update_builder_session(session_id, status="running", stage="Planning application", job_id=job_id)
            try:
                return asyncio.run(self._run(session_id, user, progress, cancelled, parent, staged_assets))
            except Exception as error:
                self.store.update_builder_session(
                    session_id,
                    status="failed",
                    stage="Builder execution failed safely",
                    latest_observation_json=json.dumps({"error": type(error).__name__}),
                )
                return {
                    "status": "execution_error",
                    "message": "The Builder session failed safely. The workspace and completed files were preserved.",
                    "workspace_id": workspace_id,
                    "workspace_preserved": True,
                    "builder_session": self.store.builder_session_public(
                        self.store.builder_session(session_id, user["id"]) or session
                    ),
                    "error": type(error).__name__,
                }

        job = self.jobs.submit(
            user["id"],
            conversation_id,
            "builder.session.execute",
            workspace_id,
            {"builder_session_id": session_id, "request": request, "mode": mode, "staged_assets": staged_assets},
            worker,
        )
        self.store.update_builder_session(session_id, job_id=str(job["job_id"]))
        if turn_key:
            self._turn_jobs[turn_key] = (str(job["job_id"]), time.monotonic())
        return {
            "status": "success",
            "queued": True,
            "message": "A durable Builder session is working on this request. Progress and the verified application will update in this chat.",
            "do_not_poll_in_this_turn": True,
            "job": job,
            "builder_session": self.store.builder_session_public(
                self.store.builder_session(session_id, user["id"]) or session
            ),
            "workspace_id": workspace_id,
            "staged_assets": staged_assets,
        }

    def _tools(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for capability_id in BUILDER_TOOL_IDS:
            decision = self.registry.authorize(capability_id, user)
            if not decision.allowed:
                continue
            item = self.registry.capabilities[capability_id]
            tools.append({
                "type": "function",
                "function": {
                    "name": self.registry.tool_name(capability_id),
                    "description": item["description"],
                    "parameters": item["arguments_schema"],
                },
            })
        return tools

    def _model(self) -> Any:
        """Create a Builder-scoped client without mutating the ordinary chat model."""

        configured = self.model_provider()
        base = configured.model if isinstance(configured, ToolCallCompatibilityModel) else None
        settings = getattr(base, "settings", None)
        if base is None or settings is None:
            return configured
        builder_settings = copy.copy(settings)
        builder_settings.model_max_tokens = max(int(settings.model_max_tokens), BUILDER_MODEL_MAX_TOKENS)
        return ToolCallCompatibilityModel(type(base)(builder_settings))

    @staticmethod
    def _system_prompt(
        session: dict[str, Any], existing_preview_id: str = "", staged_assets: list[dict[str, Any]] | None = None,
        task_state: dict[str, Any] | None = None, change_scope: str = "targeted_change",
    ) -> str:
        preview_note = (
            f"A managed preview already exists with ID {existing_preview_id}; reuse and revalidate it unless it is unhealthy."
            if existing_preview_id
            else "No preview exists yet; start exactly one after the application is ready."
        )
        assets = staged_assets or []
        if assets:
            lines = "; ".join(
                f"{item['title']} -> {item['path']}{' (most recent)' if item.get('most_recent') else ''}"
                for item in assets
            )
            asset_note = (
                "Conversation image artifacts have already been copied into this workspace, most-recent first: "
                + lines
                + ". When the user refers to this/the supplied/the generated/the previous image or asks to use an image from the conversation, use the matching staged workspace asset rather than inventing a replacement or an external URL. "
                "If an asset is requested, the final application source must actually reference its workspace-relative path; merely having the file present does not satisfy the request. "
            )
        else:
            asset_note = "No conversation image artifacts were staged for this Builder request. "
        scope_note = (
            "This request is classified substantial_change: perform broader repository orientation with "
            "builder.code.map and builder.code.search before editing, record an explicit plan via "
            "builder.task_state.update, pin more relevant files in TaskState, and expect coherent multi-file "
            "changes with a stronger quality bar. "
            if change_scope == "substantial_change" else
            "This request is classified targeted_change: keep orientation limited to what the fix requires, keep "
            "the affected-file set minimal, preserve existing architecture, and stay conservative with operations. "
        )
        state_note = (
            "Current TaskState for this session (the durable record of what is being built, what has been "
            "learned, and what remains -- keep it current with builder.task_state.update after meaningful "
            "discoveries, plan changes, or completed steps; never write hidden reasoning into it, only concrete "
            "conclusions and decisions):\n" + summarize_task_state(task_state or default_task_state())
        )
        return (
            "You are the model-directed XV12 Builder engineer: a persistent software engineer operating inside a "
            "durable, bounded execution session, not a chat model that merely calls tools. "
            "Use only the supplied Builder tools. Do not call unrelated capabilities. Do not create another workspace. "
            f"The exact owned workspace ID is {session['workspace_id']}. {preview_note} {asset_note}{scope_note}"
            "You choose the concrete architecture, files, and dependencies within the contract below. "
            "Engineering contract, in order: "
            "(1) inspect before editing -- use builder.workspace.inspect, builder.code.map, and builder.code.search "
            "to locate the real implementation path before writing code; "
            "(2) understand the existing architecture before changing it; "
            "(3) identify the actual implementation path rather than the first file that happens to compile; "
            "(4) preserve unrelated behavior; "
            "(5) reuse existing abstractions instead of inventing parallel ones; "
            "(6) avoid duplicate competing systems; "
            "(7) avoid band-aid fixes -- prefer root-cause repairs; "
            "(8) make coherent multi-file changes when the request justifies it; "
            "(9) inspect your resulting diff before calling anything done; "
            "(10) test affected functionality, not only the happiest path; "
            "(11) repair regressions before completion; "
            "(12) maintain TaskState throughout the session, not only at the end; "
            "(13) use a substantial rewrite when the user's intent genuinely requires one; "
            "(14) never optimize for the smallest change that merely clears validation when the request asked for more. "
            "For frontend work, also explicitly evaluate visual hierarchy, typography, spacing, component "
            "consistency, surfaces, depth, translucency, contrast, responsive composition, state behavior, "
            "transitions/motion, information density, and visual balance -- do not assume CSS source equals "
            "visual success. "
            "For a small website, prefer a dependency-free implementation when that satisfies the request. "
            "The default sandbox is Python 3.12 Alpine; for dependency-free sites, create a portable "
            "standard-library unittest and run it with python -m unittest rather than assuming pytest or Node "
            "packages are installed. "
            "Managed previews are deliberately network-contained: do not reference internet images, fonts, "
            "scripts, stylesheets, APIs, or CDNs. Create self-contained local files and CSS visuals so Chromium "
            "has no external failed requests. "
            "You must write or patch real files, create an applicable test, execute a bounded test or build, "
            "start or reuse the managed preview, inspect it in Chromium, repair any test/browser/runtime/network "
            "failure, and revalidate. Do not claim success from file writes alone. Keep tool observations bounded "
            "and reread only the files you need. "
            + state_note + " "
            "When all engineering and browser checks are healthy, respond with one concise completion sentence "
            "and no hidden reasoning."
        )

    @staticmethod
    def _bounded_result(capability_id: str, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {"result": str(result)[:8000]}
        bounded = dict(result)
        if capability_id == "builder.files.read" and len(str(bounded.get("content") or "")) > 14_000:
            bounded["content"] = str(bounded["content"])[:14_000]
            bounded["content_truncated"] = True
        if "summary" in bounded and len(str(bounded["summary"])) > 7000:
            bounded["summary"] = str(bounded["summary"])[-7000:]
            bounded["truncated"] = True
        if "body_text" in bounded:
            bounded["body_text"] = str(bounded["body_text"])[:4000]
        if isinstance(bounded.get("style_telemetry"), dict):
            # Bound the telemetry field itself so an element-heavy page can never push the
            # whole inspect result into the bounded_observation fallback, which would strip
            # the rendered/healthy fields that _observe reads for browser evidence.
            telemetry = bounded["style_telemetry"]
            elements = telemetry.get("elements")
            while isinstance(elements, list) and elements and len(json.dumps(telemetry, ensure_ascii=False, default=str)) > STYLE_TELEMETRY_CHAR_LIMIT:
                elements = elements[: len(elements) // 2]
                telemetry = {**telemetry, "elements": elements, "elements_truncated": True}
            bounded["style_telemetry"] = telemetry
        serialized = json.dumps(bounded, ensure_ascii=False, default=str)
        if len(serialized) > 18_000:
            return {
                "status": bounded.get("status"),
                "message": bounded.get("message"),
                "bounded_observation": serialized[:18_000],
                "truncated": True,
            }
        return bounded

    @staticmethod
    def _drop_redundant(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip repetitive successful receipts, stale browser-health confirmations, redundant
        status messages, and repeated file listings before summarization. Current errors and
        the most recent exchanges are never passed through this function -- only the older
        segment that is about to be summarized or dropped."""
        kept: list[dict[str, Any]] = []
        seen_signatures: set[str] = set()
        repetitive_tools = {"browser_preview_inspect", "builder_workspace_inspect", "builder_preview_status", "builder_code_map"}
        for message in messages:
            if message.get("role") == "tool":
                name = str(message.get("name") or "")
                try:
                    payload = json.loads(str(message.get("content") or "{}"))
                except json.JSONDecodeError:
                    payload = {}
                status = str(payload.get("status") or "")
                if name in repetitive_tools and status == "success":
                    signature = f"{name}:{status}"
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
            kept.append(message)
        return kept

    @staticmethod
    async def _summarize_engineering_history(
        model: Any, session: dict[str, Any], messages: list[dict[str, Any]],
    ) -> str:
        """Builder-specific engineering summary of older/superseded history -- distinct from
        XV12's ordinary chat rolling summary. Preserves architectural discoveries, important
        interfaces, resolved errors and their fixes, and superseded implementation attempts;
        never includes hidden reasoning, only concrete conclusions and decisions."""
        if not hasattr(model, "complete"):
            return ""
        transcript = json.dumps(messages, ensure_ascii=False, default=str)[-24_000:]
        prompt = [
            {
                "role": "system",
                "content": (
                    "Compact this Builder engineering history into a faithful, concise engineering summary. "
                    "Preserve architectural discoveries, important interfaces, resolved errors and their fixes, "
                    "and superseded implementation attempts that inform future decisions. Do not add facts. "
                    "Do not include hidden reasoning -- only concrete conclusions and decisions."
                ),
            },
            {
                "role": "user",
                "content": "Original request: " + str(session.get("original_request") or "")[:2000] + "\n\nHistory:\n" + transcript,
            },
        ]
        try:
            summary = await model.complete(prompt, max_tokens=400)
        except Exception:
            return ""
        return str(summary or "")[:3000]

    async def _compact_engineering_context(
        self, messages: list[dict[str, Any]], model: Any, session: dict[str, Any], task_state: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        """Engineering-aware retention, replacing blunt recency-based truncation.

        Always pinned: the original system contract (index 0), the current TaskState summary
        (goal, requirements, constraints, architectural discoveries, modified files, unresolved
        failures, latest critique/validation). Kept raw: the most recent tool exchanges. Older
        architecture investigation, superseded implementation attempts, and resolved errors are
        summarized rather than dropped outright; repetitive successful receipts, stale
        browser-health confirmations, and repeated file listings are dropped.
        """
        size = len(json.dumps(messages, ensure_ascii=False, default=str))
        if size <= CONTEXT_CHARACTER_LIMIT:
            return messages, size

        system = messages[:1]
        body = messages[1:]
        raw_tail = body[-BUILDER_RAW_TAIL_MESSAGES:]
        older = body[:-BUILDER_RAW_TAIL_MESSAGES]
        pruned = self._drop_redundant(older)
        summary_text = await self._summarize_engineering_history(model, session, pruned) if pruned else ""

        reconstructed = list(system)
        reconstructed.append({
            "role": "user",
            "content": (
                "Pinned engineering state (authoritative; do not contradict): \n"
                "Original request: " + str(session.get("original_request") or "")[:3000] + "\n"
                + summarize_task_state(task_state)
            ),
        })
        if summary_text:
            reconstructed.append({
                "role": "user",
                "content": "Summary of earlier engineering history (superseded detail dropped, conclusions kept): " + summary_text,
            })
        reconstructed.extend(raw_tail)
        return reconstructed, len(json.dumps(reconstructed, ensure_ascii=False, default=str))

    def _initial_evidence(
        self, workspace_id: str, user: dict[str, Any], parent: dict[str, Any] | None,
        staged_assets: list[dict[str, Any]] | None = None,
    ) -> BuilderEvidence:
        evidence = BuilderEvidence(workspace_id=workspace_id, staged_assets=list(staged_assets or []))
        if not parent or str(parent.get("workspace_id")) != workspace_id:
            return evidence
        evidence.preview_id = str(parent.get("preview_id") or "")
        artifact_id = str(parent.get("artifact_id") or "")
        if artifact_id:
            record = self.artifacts.get_owned(artifact_id, user["id"])
            if record:
                evidence.application_artifact = self.artifacts.public(record)
        return evidence

    async def _execute_tool(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        session: dict[str, Any],
        user: dict[str, Any],
        evidence: BuilderEvidence,
    ) -> dict[str, Any]:
        if capability_id not in BUILDER_TOOL_IDS:
            return {"status": "permission_denied", "message": "This tool is outside the Builder session surface."}
        workspace_tools = {
            "builder.workspace.inspect", "builder.files.read", "builder.files.patch", "builder.files.batch",
            "builder.sandbox.exec", "builder.preview.start", "builder.project.archive", "git.status", "git.diff",
            "builder.code.search", "builder.code.map",
        }
        if capability_id in workspace_tools:
            supplied = str(arguments.get("workspace_id") or session["workspace_id"])
            if supplied != str(session["workspace_id"]):
                return {"status": "permission_denied", "message": "Builder sessions cannot switch workspaces."}
            arguments["workspace_id"] = str(session["workspace_id"])
        if capability_id == "builder.task_state.update":
            arguments["session_id"] = str(session["id"])
        if capability_id in {"builder.sandbox.exec", "builder.preview.start", "browser.preview.screenshot", "builder.project.archive"}:
            arguments["conversation_id"] = str(session["conversation_id"])
        preview_tools = {"builder.preview.status", "builder.preview.stop", "browser.preview.inspect", "browser.preview.screenshot"}
        if capability_id in preview_tools and not arguments.get("preview_id"):
            arguments["preview_id"] = evidence.preview_id
        if capability_id in preview_tools and str(arguments.get("preview_id") or "") != evidence.preview_id:
            return {"status": "permission_denied", "message": "Builder sessions may use only their owned managed preview."}
        if capability_id == "builder.preview.start" and evidence.preview_id:
            status, _ = await self.gateway.execute("builder.preview.status", user, {"preview_id": evidence.preview_id})
            if isinstance(status, dict) and (status.get("preview") or {}).get("state") == "running":
                return {"status": "success", "preview": status["preview"], "reused_existing_preview": True}
        if capability_id == "browser.preview.inspect" and evidence.browser_healthy:
            return {
                "status": "success", "rendered": True, "healthy": True, "reused_healthy_evidence": True,
                "message": "Chromium is already healthy for the current files. Complete the remaining verification gates instead of repeating browser inspection.",
                "remaining_gates": evidence.missing(),
            }

        result, decision = await self.gateway.execute(capability_id, user, arguments)
        bounded = self._bounded_result(capability_id, result)
        if isinstance(bounded, dict):
            bounded = {"authorization": decision.reason, **bounded}
        self._observe(capability_id, bounded, evidence)
        return bounded

    @staticmethod
    def _observe(capability_id: str, result: dict[str, Any], evidence: BuilderEvidence) -> None:
        success = result.get("status") == "success"
        if capability_id in {"builder.files.patch", "builder.files.batch"} and success:
            evidence.files_changed = True
            evidence.sandbox_succeeded = False
            if evidence.browser_failed:
                evidence.repair_cycles += 1
                evidence.browser_failed = False
                evidence.browser_healthy = False
            elif evidence.browser_healthy:
                evidence.browser_healthy = False
        elif capability_id == "builder.sandbox.exec":
            if success and result.get("executed") is True and int(result.get("exit_code") or 0) == 0:
                evidence.sandbox_succeeded = True
            evidence.test_build_summary = str(result.get("summary") or "")[-4000:]
        elif capability_id == "builder.preview.start" and success:
            preview = result.get("preview") or {}
            evidence.preview_id = str(preview.get("id") or evidence.preview_id)
            if result.get("artifact"):
                evidence.application_artifact = result["artifact"]
        elif capability_id == "browser.preview.inspect":
            evidence.browser_cycles += 1
            evidence.browser_healthy = bool(success and result.get("rendered") and result.get("healthy"))
            evidence.browser_failed = not evidence.browser_healthy
            evidence.browser_title = str(result.get("title") or "")[:500]
            evidence.browser_body_text = str(result.get("body_text") or "")[:5000]
            if result.get("style_telemetry"):
                evidence.style_telemetry = json.dumps(result["style_telemetry"], ensure_ascii=False, default=str)[:STYLE_TELEMETRY_CHAR_LIMIT + 2000]
        elif capability_id == "browser.preview.screenshot" and success:
            evidence.screenshot_artifact = result.get("artifact")
        elif capability_id == "builder.project.archive" and success:
            evidence.archive_artifact = result.get("artifact")
        evidence.latest_observations.append({
            "capability_id": capability_id,
            "status": result.get("status"),
            "summary": str(result.get("message") or result.get("summary") or "")[-1200:],
        })
        evidence.latest_observations = evidence.latest_observations[-12:]

    @staticmethod
    def _apply_deterministic_task_state(
        task_state: dict[str, Any],
        assistant_calls: list[dict[str, Any]],
        tool_messages: list[dict[str, Any]],
        evidence: BuilderEvidence,
    ) -> dict[str, Any]:
        """Update the mechanical, evidence-backed fields of TaskState after a tool-call batch:
        changed files, current failures, and latest validation. These fields are never
        model-writable via builder.task_state.update -- they come only from actual tool
        results, so the model cannot fabricate validation state in TaskState the way the
        engineering contract already forbids it from claiming success from file writes alone.
        """
        changed_files = list(task_state.get("changed_files") or [])
        current_failures: list[str] = []
        for call, message in zip(assistant_calls, tool_messages):
            name = str((call.get("function") or {}).get("name") or "")
            try:
                arguments = json.loads(str((call.get("function") or {}).get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            try:
                result = json.loads(str(message.get("content") or "{}"))
            except json.JSONDecodeError:
                result = {}
            status = str(result.get("status") or "")
            if name == "builder_files_patch" and status == "success":
                path = str(arguments.get("path") or "")
                if path and path not in changed_files:
                    changed_files.append(path)
            elif name == "builder_files_batch" and status == "success":
                for path in result.get("paths") or []:
                    if str(path) not in changed_files:
                        changed_files.append(str(path))
            elif name == "builder_sandbox_exec":
                healthy = status == "success" and result.get("executed") is True and int(result.get("exit_code") or 1) == 0
                if not healthy:
                    current_failures.append(("sandbox: " + str(result.get("summary") or result.get("message") or "test/build failed"))[:400])
            elif name == "browser_preview_inspect":
                if not result.get("healthy"):
                    errors = result.get("runtime_errors") or []
                    failure = "; ".join(str(item) for item in errors)[:300] or "browser validation unhealthy"
                    current_failures.append(("browser: " + failure)[:400])
        patch: dict[str, Any] = {"changed_files": changed_files}
        if current_failures:
            patch["current_failures"] = current_failures
        elif evidence.browser_healthy and evidence.sandbox_succeeded:
            patch["current_failures"] = []
        patch["latest_validation"] = {
            "files_changed": evidence.files_changed, "sandbox_passed": evidence.sandbox_succeeded,
            "browser_healthy": evidence.browser_healthy,
        }
        return merge_task_state(task_state, patch)

    def _persist(self, session_id: str, evidence: BuilderEvidence, started: float, stage: str, status: str = "running") -> None:
        self.store.update_builder_session(
            session_id,
            status=status,
            stage=stage,
            operation_count=evidence.operation_count,
            model_rounds=evidence.model_rounds,
            repair_cycles=evidence.repair_cycles,
            browser_cycles=evidence.browser_cycles,
            elapsed_seconds=round(time.monotonic() - started, 3),
            latest_observation_json=json.dumps(evidence.latest_observations, ensure_ascii=False),
            preview_id=evidence.preview_id,
            artifact_id=str((evidence.application_artifact or {}).get("id") or ""),
        )

    @staticmethod
    async def _review_requirements(
        model: Any, session: dict[str, Any], evidence: BuilderEvidence, messages: list[dict[str, Any]],
    ) -> tuple[bool, list[str]]:
        if not hasattr(model, "complete"):
            return True, []
        record = json.dumps(messages[-10:], ensure_ascii=False, default=str)[:16_000]
        asset_evidence = json.dumps(
            {"staged_assets": evidence.staged_assets, "asset_usage": evidence.asset_usage},
            ensure_ascii=False, default=str,
        )[:12_000]
        prompt = (
            "Original application request:\n" + str(session["original_request"])[:6000]
            + "\n\nFinal Chromium title:\n" + evidence.browser_title
            + "\n\nFinal visible text:\n" + evidence.browser_body_text
            + "\n\nConversation asset evidence:\n" + asset_evidence
            + "\n\nRecent bounded engineering record:\n" + record
        )
        raw = await model.complete([
            {
                "role": "system",
                "content": (
                    "You are the XV12 Builder acceptance reviewer. Compare the original request with the final rendered UI, conversation asset evidence, and engineering record. "
                    "Return strict JSON only: {\"satisfied\":true|false,\"missing\":[\"short concrete requirement\"]}. "
                    "Mark false when any explicit requested section, behavior, visual change, content, or requested conversation asset use is absent. "
                    "A requested visible section or content must be present in the final Chromium-visible text; unused CSS selectors, comments, planned classes, or tool arguments do not count. "
                    "If the user asks to use this/the supplied/the generated/the previous image or another conversation asset, the matching staged asset must show referenced=true with at least one real application source file in referenced_by. Merely staging or copying the asset does not count as use. "
                    "Use engineering records only to verify non-textual implementation details such as an explicitly requested color. Do not require anything the user did not request."
                ),
            },
            {"role": "user", "content": prompt},
        ], max_tokens=512)
        cleaned = str(raw).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            return False, ["The final requirement review did not return a valid verdict."]
        missing = [str(item)[:500] for item in result.get("missing") or [] if str(item).strip()][:12]
        return bool(result.get("satisfied") is True and not missing), missing

    def _build_diff_context(self, workspace_id: str, user: dict[str, Any], changed_files: list[str]) -> str:
        """Build a content-based diff-review context for the quality critique. Builder
        workspaces are never `git init`'d, so a real `git diff` is not reliably available
        (see the Phase 2 handoff finding); this instead reads the current content of each
        TaskState-tracked changed file. It is a content diff, not a line diff, but it is
        grounded in real files rather than the model's own claims about what it changed."""
        sections: list[str] = []
        total = 0
        for path in changed_files[:DIFF_CONTEXT_FILE_LIMIT]:
            try:
                result = self.workspaces.read({"workspace_id": workspace_id, "path": path}, user)
                content = str(result.get("content") or "")[:DIFF_CONTEXT_FILE_CHAR_LIMIT]
                section = f"--- {path} ---\n{content}"
            except Exception as error:
                section = f"--- {path} ---\n(unreadable: {str(error)[:200]})"
            if total + len(section) > DIFF_CONTEXT_TOTAL_CHAR_LIMIT:
                break
            sections.append(section)
            total += len(section)
        return "\n\n".join(sections)

    @staticmethod
    async def _review_quality(
        model: Any, session: dict[str, Any], evidence: BuilderEvidence,
        task_state: dict[str, Any], diff_context: str,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Architecture/quality critique, run once requirements review has passed. Distinct
        from _review_requirements: that method checks whether the requested behavior exists;
        this one checks whether the way it was built is sound."""
        if not hasattr(model, "complete"):
            return True, []
        prompt = (
            "Original application request:\n" + str(session["original_request"])[:6000]
            + "\n\nChange scope: " + str(task_state.get("change_scope") or "")
            + "\n\nTaskState summary:\n" + summarize_task_state(task_state)
            + "\n\nChanged file contents (content diff -- workspaces are not git repositories, "
            "so this is current file content, not a unified diff):\n"
            + (diff_context or "(no changed files recorded)")
            + "\n\nFinal Chromium title:\n" + evidence.browser_title
            + "\n\nFinal visible text:\n" + evidence.browser_body_text
            + "\n\nComputed style telemetry from the final Chromium validation (per visible element; "
            "keys omitted at defaults per its defaults_omitted note):\n"
            + (evidence.style_telemetry or "(no style telemetry captured)")
            + "\n\nTest/build summary:\n" + evidence.test_build_summary[-2000:]
        )
        raw = await model.complete([
            {
                "role": "system",
                "content": (
                    "You are the XV12 Builder quality and architecture critic. Review the changed files against "
                    "the original request and TaskState. Check: did the change actually solve the request; "
                    "architectural coherence with the rest of the codebase; duplicated logic; band-aid fixes "
                    "instead of root-cause repairs; whether unrelated existing behavior was preserved; whether "
                    "tests adequately cover the change; whether the magnitude of the change matched what was "
                    "requested (no unrequested rewrites, no missing requested scope); unnecessary complexity or "
                    "abstraction; unresolved regressions; and, for UI-facing changes, whether the requested "
                    "visual characteristics were actually achieved -- judge visual claims against the computed "
                    "style telemetry (actual rendered colors, alpha, blur, spacing, overlap, overflow), not "
                    "against what the CSS source appears to intend. "
                    "Return strict JSON only: {\"acceptable\":true|false,\"issues\":[{\"type\":\"...\","
                    "\"severity\":\"low|medium|high\",\"finding\":\"...\",\"recommended_repair\":\"...\"}]}. "
                    "Only report issues you can support from the given content -- do not speculate about files "
                    "you cannot see."
                ),
            },
            {"role": "user", "content": prompt},
        ], max_tokens=768)
        cleaned = str(raw).strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            return False, [{
                "type": "critique_error", "severity": "medium",
                "finding": "The quality critique did not return a valid verdict.",
                "recommended_repair": "Retry the critique after the next repair cycle.",
            }]
        issues: list[dict[str, Any]] = []
        for item in (result.get("issues") or [])[:QUALITY_CRITIQUE_ISSUE_LIMIT]:
            if not isinstance(item, dict):
                continue
            issues.append({
                "type": str(item.get("type") or "")[:80],
                "severity": str(item.get("severity") or "")[:20],
                "finding": str(item.get("finding") or "")[:500],
                "recommended_repair": str(item.get("recommended_repair") or "")[:500],
            })
        acceptable = bool(result.get("acceptable") is True and not issues)
        return acceptable, issues

    def _partial_result(self, session: dict[str, Any], user: dict[str, Any], evidence: BuilderEvidence, reason: str) -> dict[str, Any]:
        self.store.update_builder_session(
            str(session["id"]), status="partial_success", stage="Builder stopped at a safe bound",
            operation_count=evidence.operation_count, model_rounds=evidence.model_rounds,
            repair_cycles=evidence.repair_cycles, browser_cycles=evidence.browser_cycles,
            latest_observation_json=json.dumps(evidence.latest_observations, ensure_ascii=False),
            preview_id=evidence.preview_id, artifact_id=str((evidence.application_artifact or {}).get("id") or ""),
        )
        result = {
            "status": "partial_success",
            "message": "Builder reached its workflow-specific bound. Completed work was preserved and can be continued.",
            "workspace_id": evidence.workspace_id,
            "workspace_preserved": True,
            "preview_available": bool(evidence.preview_id),
            "blocking_issue": reason,
            "operations_completed": evidence.operation_count,
            "next_recommended_action": "Continue the same Builder project in this conversation.",
            "builder_session": self.store.builder_session_public(
                self.store.builder_session(str(session["id"]), user["id"]) or session
            ),
            "staged_assets": evidence.staged_assets,
            "asset_usage": evidence.asset_usage,
        }
        if evidence.application_artifact:
            result["artifact"] = evidence.application_artifact
        return result

    async def _run(
        self,
        session_id: str,
        user: dict[str, Any],
        progress: Callable[[int, str], None],
        cancelled: Callable[[], bool],
        parent: dict[str, Any] | None,
        staged_assets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        session = self.store.builder_session(session_id, user["id"])
        if not session:
            raise RuntimeError("Builder session disappeared before execution.")
        started = time.monotonic()
        evidence = self._initial_evidence(str(session["workspace_id"]), user, parent, staged_assets)
        has_existing_workspace = bool(parent and str(parent.get("workspace_id")) == str(session["workspace_id"]))
        change_scope = classify_change_scope(str(session["original_request"]), str(session["mode"]), has_existing_workspace)
        if has_existing_workspace:
            # Carry the prior TaskState forward so a follow-up modification retains
            # architecture and intent instead of rediscovering the whole task.
            task_state = merge_task_state(self.task_state.load(parent), {
                "goal": str(session["original_request"]), "change_scope": change_scope,
                "current_failures": [], "latest_critique": [],
            })
        else:
            task_state = default_task_state(str(session["original_request"]), change_scope)
        task_state = self.task_state.save(session_id, task_state)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(session, evidence.preview_id, evidence.staged_assets, task_state, change_scope)},
            {"role": "user", "content": str(session["original_request"])},
        ]
        tools = self._tools(user)
        if not tools:
            return self._partial_result(session, user, evidence, "No authorized Builder engineering tools are available.")
        model = self._model()

        for round_index in range(MODEL_ROUND_LIMIT):
            if cancelled():
                if evidence.preview_id:
                    try:
                        await self.gateway.execute("builder.preview.stop", user, {"preview_id": evidence.preview_id})
                    except Exception:
                        pass
                self.store.update_builder_session(session_id, status="cancelled", stage="Cancelled; workspace preserved")
                return {"status": "cancelled", "workspace_id": evidence.workspace_id, "workspace_preserved": True}
            if time.monotonic() - started > WALL_TIME_LIMIT_SECONDS:
                return self._partial_result(session, user, evidence, "Builder wall-time budget reached.")
            if evidence.operation_count >= HARD_OPERATION_LIMIT:
                return self._partial_result(session, user, evidence, "Builder hard operation budget reached.")
            if evidence.repair_cycles > REPAIR_CYCLE_LIMIT or (
                evidence.browser_cycles >= BROWSER_CYCLE_LIMIT and not evidence.browser_healthy
            ):
                return self._partial_result(session, user, evidence, "Builder validation or repair-cycle budget reached.")

            evidence.model_rounds = round_index + 1
            progress(min(12 + round_index * 4, 82), "Planning and implementing application" if round_index < 2 else "Testing and validating application")
            calls: list[dict[str, Any]] = []
            content: list[str] = []
            async for event in model.stream_events(messages, tools=tools):
                if event.get("type") == "tool_call":
                    calls.append(event)
                elif event.get("type") == "content":
                    content.append(str(event.get("text") or ""))

            if calls:
                assistant_calls: list[dict[str, Any]] = []
                tool_messages: list[dict[str, Any]] = []
                for call in calls:
                    if evidence.operation_count >= HARD_OPERATION_LIMIT:
                        break
                    call_id = str(call.get("id") or f"builder_{uuid.uuid4().hex}")
                    tool_name = str(call.get("name") or "")
                    try:
                        capability_id = self.registry.capability_id_for_tool(tool_name)
                        arguments = json.loads(str(call.get("arguments") or "{}"))
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be an object.")
                        evidence.operation_count += 1
                        result = await self._execute_tool(capability_id, arguments, session, user, evidence)
                    except Exception as error:
                        capability_id = tool_name
                        arguments = {}
                        result = {"status": "invalid_arguments", "message": str(error)[:1000]}
                    assistant_calls.append({
                        "id": call_id, "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                    })
                    tool_messages.append({
                        "role": "tool", "tool_call_id": call_id, "name": tool_name,
                        "content": json.dumps(result, ensure_ascii=False, default=str)[:18_000],
                    })
                    self._persist(session_id, evidence, started, "Executing Builder engineering loop")
                messages.append({"role": "assistant", "content": None, "tool_calls": assistant_calls})
                messages.extend(tool_messages)
                # TaskState may already have been updated this batch via builder.task_state.update;
                # reload before layering the deterministic, evidence-backed fields on top.
                session_row = self.store.builder_session(session_id, user["id"]) or session
                task_state = self.task_state.load(session_row)
                task_state = self._apply_deterministic_task_state(task_state, assistant_calls, tool_messages, evidence)
                task_state = self.task_state.save(session_id, task_state)
                missing = evidence.missing()
                messages.append({
                    "role": "user",
                    "content": (
                        "Deterministic verification state after this batch: "
                        f"files_changed={evidence.files_changed}; sandbox_passed={evidence.sandbox_succeeded}; "
                        f"preview_ready={bool(evidence.preview_id and evidence.application_artifact)}; "
                        f"browser_healthy={evidence.browser_healthy}. "
                        + ("Complete these remaining gates next: " + "; ".join(missing) + ". " if missing else "All required gates are satisfied. ")
                        + "Keep the original request in scope: " + str(session["original_request"])[:3000] + ". "
                        + "If the request depends on a staged conversation asset, ensure the actual application source references its listed workspace-relative path. "
                        + "Do not repeat a healthy gate unless a later file change invalidates its evidence.\n"
                        + "Current TaskState:\n" + summarize_task_state(task_state)
                    ),
                })
                messages, context_size = await self._compact_engineering_context(messages, model, session, task_state)
                self.store.update_builder_session(session_id, generated_context_size=context_size)
                if evidence.operation_count >= SOFT_OPERATION_LIMIT:
                    progress(84, "Completing final bounded validation")
                continue

            missing = evidence.missing()
            if missing:
                messages.append({
                    "role": "user",
                    "content": "Verification is incomplete. Continue using Builder tools and address: " + "; ".join(missing) + ".",
                })
                messages, context_size = await self._compact_engineering_context(messages, model, session, task_state)
                self.store.update_builder_session(session_id, generated_context_size=context_size)
                continue

            evidence.asset_usage = self._scan_asset_usage(evidence.workspace_id, user["id"], evidence.staged_assets)
            evidence.requirements_review_cycles += 1
            satisfied, semantic_missing = await self._review_requirements(model, session, evidence, messages)
            if not satisfied:
                evidence.latest_observations.append({
                    "capability_id": "builder.requirements.review", "status": "missing_requirements",
                    "summary": "; ".join(semantic_missing)[:1200],
                })
                evidence.latest_observations = evidence.latest_observations[-12:]
                task_state = self.task_state.save(session_id, merge_task_state(task_state, {
                    "latest_critique": semantic_missing or ["review verdict was incomplete"],
                    "current_failures": list({*task_state.get("current_failures", []), *semantic_missing}),
                }))
                messages.append({
                    "role": "user",
                    "content": (
                        "Final acceptance review found missing requested behavior, content, or asset usage: "
                        + "; ".join(semantic_missing or ["review verdict was incomplete"])
                        + ". Repair the application, rerun its test/build, and revalidate Chromium before finishing. "
                        + "Current staged asset usage evidence: " + json.dumps(evidence.asset_usage, ensure_ascii=False)[:5000]
                    ),
                })
                continue

            # Critique-driven (including visual) repairs are bounded by MODEL_ROUND_LIMIT,
            # HARD_OPERATION_LIMIT, and wall time -- deliberately not by REPAIR_CYCLE_LIMIT,
            # which counts technical regressions (a file change after a failed browser
            # validation). A quality repair starts from a healthy browser, so it is a
            # separate gate sharing the round budget, not a regression.
            evidence.quality_review_cycles += 1
            diff_context = self._build_diff_context(evidence.workspace_id, user, task_state.get("changed_files") or [])
            acceptable, quality_issues = await self._review_quality(model, session, evidence, task_state, diff_context)
            if not acceptable:
                evidence.latest_observations.append({
                    "capability_id": "builder.quality.critique", "status": "quality_issues_found",
                    "summary": "; ".join(str(issue.get("finding") or "") for issue in quality_issues)[:1200],
                })
                evidence.latest_observations = evidence.latest_observations[-12:]
                critique_lines = [
                    f"{issue.get('severity', '')}: {issue.get('finding', '')} -> {issue.get('recommended_repair', '')}"
                    for issue in quality_issues
                ] or ["quality critique verdict was incomplete"]
                task_state = self.task_state.save(session_id, merge_task_state(task_state, {
                    "latest_critique": critique_lines,
                    "current_failures": list({*task_state.get("current_failures", []), *critique_lines}),
                }))
                messages.append({
                    "role": "user",
                    "content": (
                        "Quality and architecture critique found issues that must be repaired before finishing: "
                        + json.dumps(quality_issues, ensure_ascii=False)[:5000]
                        + ". Address each recommended repair, rerun tests/build, and revalidate Chromium before finishing."
                    ),
                })
                continue

            task_state = self.task_state.save(session_id, merge_task_state(task_state, {
                "current_failures": [], "latest_critique": [], "next_action": "Capture final evidence and deliver.",
            }))
            break
        else:
            return self._partial_result(session, user, evidence, "Builder model-round budget reached.")

        evidence.asset_usage = self._scan_asset_usage(evidence.workspace_id, user["id"], evidence.staged_assets)
        progress(88, "Capturing final preview evidence")
        if not evidence.screenshot_artifact:
            if evidence.operation_count >= HARD_OPERATION_LIMIT:
                return self._partial_result(session, user, evidence, "No operation budget remained for final screenshot.")
            evidence.operation_count += 1
            result = await self._execute_tool(
                "browser.preview.screenshot", {"title": "Verified application preview"}, session, user, evidence
            )
            if result.get("status") != "success":
                return self._partial_result(session, user, evidence, "Final screenshot validation failed.")
        progress(92, "Packaging project download")
        if not evidence.archive_artifact:
            if evidence.operation_count >= HARD_OPERATION_LIMIT:
                return self._partial_result(session, user, evidence, "No operation budget remained for the project archive.")
            evidence.operation_count += 1
            result = await self._execute_tool("builder.project.archive", {}, session, user, evidence)
            if result.get("status") != "success":
                return self._partial_result(session, user, evidence, "Project archive creation failed.")

        progress(96, "Finalizing live chat preview")
        application = self.previews.finalize_artifact(
            preview_id=evidence.preview_id,
            user=user,
            conversation_id=str(session["conversation_id"]),
            title=str(self.store.workspace(str(session["workspace_id"]), user["id"])["name"]),
            screenshot=evidence.screenshot_artifact,
            project_archive=evidence.archive_artifact,
            validation={
                "healthy": evidence.browser_healthy,
                "browser_cycles": evidence.browser_cycles,
                "repair_cycles": evidence.repair_cycles,
                "requirements_reviewed": True,
                "requirements_review_cycles": evidence.requirements_review_cycles,
                "quality_reviewed": True,
                "quality_review_cycles": evidence.quality_review_cycles,
                "test_build_summary": evidence.test_build_summary[-2000:],
                "asset_usage": evidence.asset_usage,
            },
        )
        evidence.application_artifact = application
        self.store.update_builder_session(
            session_id, status="succeeded", stage="Complete", operation_count=evidence.operation_count,
            model_rounds=evidence.model_rounds, repair_cycles=evidence.repair_cycles,
            browser_cycles=evidence.browser_cycles, elapsed_seconds=round(time.monotonic() - started, 3),
            latest_observation_json=json.dumps(evidence.latest_observations, ensure_ascii=False),
            preview_id=evidence.preview_id, artifact_id=str(application["id"]),
        )
        public_session = self.store.builder_session_public(self.store.builder_session(session_id, user["id"]) or session)
        return {
            "status": "success",
            "message": f"{application['title']} is ready. The application was built, tested, browser-validated, and attached below.",
            "workspace_id": evidence.workspace_id,
            "preview_id": evidence.preview_id,
            "artifact": application,
            "artifacts": [application, evidence.screenshot_artifact, evidence.archive_artifact],
            "screenshot": evidence.screenshot_artifact,
            "project_archive": evidence.archive_artifact,
            "validation": {
                "healthy": True, "browser_cycles": evidence.browser_cycles,
                "repair_cycles": evidence.repair_cycles,
                "requirements_reviewed": True,
                "requirements_review_cycles": evidence.requirements_review_cycles,
                "quality_reviewed": True,
                "quality_review_cycles": evidence.quality_review_cycles,
                "asset_usage": evidence.asset_usage,
            },
            "staged_assets": evidence.staged_assets,
            "asset_usage": evidence.asset_usage,
            "operations_completed": evidence.operation_count,
            "model_rounds": evidence.model_rounds,
            "workspace_preserved": True,
            "builder_session": public_session,
        }