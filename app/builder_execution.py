from __future__ import annotations

import asyncio
import copy
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .model_compat import ToolCallCompatibilityModel


BUILDER_TOOL_IDS = (
    "builder.workspace.inspect",
    "builder.files.read",
    "builder.files.patch",
    "builder.files.batch",
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

    def execute(self, arguments: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
        request = str(arguments.get("request") or "").strip()
        if not request:
            return {"status": "invalid_arguments", "message": "A Builder request is required."}
        conversation_id = str(arguments.get("conversation_id") or "").strip()
        if not conversation_id:
            from .artifacts import active_conversation_id

            conversation_id = str(active_conversation_id() or "")
        if not conversation_id:
            return {"status": "invalid_arguments", "message": "Builder execution requires an active conversation."}

        mode = str(arguments.get("mode") or "build").casefold()
        if mode not in {"build", "modify", "repair", "continue"}:
            return {"status": "invalid_arguments", "message": "Builder mode must be build, modify, repair, or continue."}
        parent = self.store.latest_builder_session(user["id"], conversation_id)
        workspace_id = str(arguments.get("workspace_id") or "")
        force_new_workspace = bool(arguments.get("new_workspace"))
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
                return asyncio.run(self._run(session_id, user, progress, cancelled, parent))
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
            {"builder_session_id": session_id, "request": request, "mode": mode},
            worker,
        )
        self.store.update_builder_session(session_id, job_id=str(job["job_id"]))
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
    def _system_prompt(session: dict[str, Any], existing_preview_id: str = "") -> str:
        preview_note = (
            f"A managed preview already exists with ID {existing_preview_id}; reuse and revalidate it unless it is unhealthy."
            if existing_preview_id
            else "No preview exists yet; start exactly one after the application is ready."
        )
        return (
            "You are the model-directed XV12 Builder engineer operating inside a durable, bounded execution session. "
            "Use only the supplied Builder tools. Do not call unrelated capabilities. Do not create another workspace. "
            f"The exact owned workspace ID is {session['workspace_id']}. {preview_note} "
            "Implement the user's request as a polished, responsive, interactive application. You choose the architecture, files, dependencies, and repair strategy. "
            "For a small website, prefer a dependency-free implementation when that satisfies the request. "
            "The default sandbox is Python 3.12 Alpine; for dependency-free sites, create a portable standard-library unittest and run it with python -m unittest rather than assuming pytest or Node packages are installed. "
            "Managed previews are deliberately network-contained: do not reference internet images, fonts, scripts, stylesheets, APIs, or CDNs. Create self-contained local files and CSS visuals so Chromium has no external failed requests. "
            "You must write or patch real files, create an applicable test, execute a bounded test or build, start or reuse the managed preview, inspect it in Chromium, repair any test/browser/runtime/network failure, and revalidate. "
            "Do not claim success from file writes alone. Keep tool observations bounded and reread only the files you need. "
            "When all engineering and browser checks are healthy, respond with one concise completion sentence and no hidden reasoning."
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
    def _compact_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        size = len(json.dumps(messages, ensure_ascii=False, default=str))
        if size <= CONTEXT_CHARACTER_LIMIT:
            return messages, size
        compact = messages[:2] + messages[-14:]
        return compact, len(json.dumps(compact, ensure_ascii=False, default=str))

    def _initial_evidence(self, workspace_id: str, user: dict[str, Any], parent: dict[str, Any] | None) -> BuilderEvidence:
        evidence = BuilderEvidence(workspace_id=workspace_id)
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
        }
        if capability_id in workspace_tools:
            supplied = str(arguments.get("workspace_id") or session["workspace_id"])
            if supplied != str(session["workspace_id"]):
                return {"status": "permission_denied", "message": "Builder sessions cannot switch workspaces."}
            arguments["workspace_id"] = str(session["workspace_id"])
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
        prompt = (
            "Original application request:\n" + str(session["original_request"])[:6000]
            + "\n\nFinal Chromium title:\n" + evidence.browser_title
            + "\n\nFinal visible text:\n" + evidence.browser_body_text
            + "\n\nRecent bounded engineering record:\n" + record
        )
        raw = await model.complete([
            {
                "role": "system",
                "content": (
                    "You are the XV12 Builder acceptance reviewer. Compare the original request with the final rendered UI and engineering record. "
                    "Return strict JSON only: {\"satisfied\":true|false,\"missing\":[\"short concrete requirement\"]}. "
                    "Mark false when any explicit requested section, behavior, visual change, or content is absent. "
                    "A requested visible section or content must be present in the final Chromium-visible text; unused CSS selectors, comments, planned classes, or tool arguments do not count. "
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
    ) -> dict[str, Any]:
        session = self.store.builder_session(session_id, user["id"])
        if not session:
            raise RuntimeError("Builder session disappeared before execution.")
        started = time.monotonic()
        evidence = self._initial_evidence(str(session["workspace_id"]), user, parent)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt(session, evidence.preview_id)},
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
                        + "Do not repeat a healthy gate unless a later file change invalidates its evidence."
                    ),
                })
                messages, context_size = self._compact_messages(messages)
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
                messages, context_size = self._compact_messages(messages)
                self.store.update_builder_session(session_id, generated_context_size=context_size)
                continue

            evidence.requirements_review_cycles += 1
            satisfied, semantic_missing = await self._review_requirements(model, session, evidence, messages)
            if not satisfied:
                evidence.latest_observations.append({
                    "capability_id": "builder.requirements.review", "status": "missing_requirements",
                    "summary": "; ".join(semantic_missing)[:1200],
                })
                evidence.latest_observations = evidence.latest_observations[-12:]
                messages.append({
                    "role": "user",
                    "content": (
                        "Final acceptance review found missing requested behavior or content: "
                        + "; ".join(semantic_missing or ["review verdict was incomplete"])
                        + ". Repair the application, rerun its test/build, and revalidate Chromium before finishing."
                    ),
                })
                continue

            break
        else:
            return self._partial_result(session, user, evidence, "Builder model-round budget reached.")

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
                "test_build_summary": evidence.test_build_summary[-2000:],
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
            },
            "operations_completed": evidence.operation_count,
            "model_rounds": evidence.model_rounds,
            "workspace_preserved": True,
            "builder_session": public_session,
        }
