# XV12 Frontier-Agent Upgrade — Handoff for Phase 2/3 (Claude Code)

> **Status update (2026-08-12, Claude Code):** Phases 2 and 3 below are now IMPLEMENTED and
> the full pytest suite is green. Phase 1 was verified by real test runs; the only
> Phase-1-caused failure was a hardcoded `registry_version` assertion (fixed). The
> GitService finding in section 2 was resolved with **option 2** (content diff from
> TaskState `changed_files` via `_build_diff_context()`; no `git init`). The quality
> critique is `BuilderExecutionService._review_quality()`, wired into `_run()` after
> `_review_requirements()`; critique-driven repairs are bounded by the shared
> round/operation/wall-time budgets, deliberately NOT by `REPAIR_CYCLE_LIMIT` (separate-gate
> decision, section 3 item 5). Style telemetry is `BrowserService._STYLE_TELEMETRY_JS` in
> `app/creator.py`, strictly additive and fail-soft, verified against real Chromium, and is
> injected into the critique prompt via `BuilderEvidence.style_telemetry`. Phase 4 remains
> not started (hardware benchmarking, per section 5).

Continuation of `XV12 Frontier-Agent Upgrade Handoff` (Phase 1: Agent Intelligence). This
doc was written from inside Cowork mode, which has **no working shell/git/test execution**
in this environment (sandbox VM fails to start — Windows HCS `Access is denied`, persistent
across retries). Everything below Phase 1 was written and manually re-read for consistency,
but **nothing has been executed**. Your first job is verification, not new code.

## 0. First thing to do: verify Phase 1 didn't break anything

```powershell
cd C:\Users\otisd\Projects\XV12   # or wherever this checkout actually lives — confirm with `git remote -v`
.\runtime\python\Scripts\python.exe -m pytest tests/ -q
```

If the repo isn't a git checkout at that path, find it — this doc was built against a
connected folder at `X:\XV12`. Confirm the working tree matches what's described below via
`git status` / `git diff` before trusting any of this summary.

Highest-risk tests to check first (most coupled to the Phase 1 changes):
- `tests/test_builder_execution_session.py` — full `_run()` loop, mocked models
  (`ModelDirectedBuilder`, `ContinuationBuilder`, `EndlessBuilder`, `SlowBuilder`). These
  mocks script tool calls by round index and ignore prompt/tool-list content, so Phase 1's
  system-prompt rewrite and TaskState injection *should* be invisible to them — but this is
  exactly the kind of assumption that needs a real test run, not just code review.
- `tests/test_creator_stack.py` — `test_creator_registry_permissions_and_health_are_truthful`
  uses `required <= ids` (subset check) so the 3 new capability IDs shouldn't break it, but confirm.
- `tests/test_permissions.py`, `tests/test_capability_truth_inventory.py` — registry-wide checks.

If anything fails, fix forward — don't revert Phase 1 without understanding why first, since
the failure might just be revealing an assumption I got wrong from static reading.

## 1. Phase 1 — what's actually in the repo right now

All in `X:\XV12` (paths below are relative to repo root):

**`config/capabilities.v1.json`**
- `registry_version` bumped `4.1.0` → `4.2.0`.
- Added `builder.code.search` (after `builder.files.batch`), `builder.code.map` (same
  spot), `builder.task_state.update` (after `builder.project.archive`, before `git.status`).
  All three: `risk_tier: 0`, `model_exposed: false`, `authorization.roles: [admin, user]` —
  same pattern as the other Builder-internal tools.

**`app/builder_execution.py`**
- `BUILDER_TOOL_IDS` extended with the 3 new capability IDs.
- New module-level TaskState machinery: `default_task_state()`, `parse_task_state()`,
  `merge_task_state()`, `summarize_task_state()`, `classify_change_scope()`,
  `TaskStateService` class. TaskState schema matches the original doc's spec exactly
  (`goal`, `change_scope`, `requirements`, `constraints`, `architecture{entry_points,
  components, interfaces, important_files}`, `plan`, `completed`, `open_items`,
  `changed_files`, `current_failures`, `latest_validation`, `latest_critique`, `next_action`).
- **Design decision**: `changed_files`, `current_failures`, and `latest_validation` are
  *never* model-writable — `_apply_deterministic_task_state()` (a new `@staticmethod`) sets
  them from actual tool results after every batch. Everything else (`goal`, `plan`,
  `requirements`, `constraints`, `architecture`, `completed`, `open_items`, `next_action`) is
  writable by the model via the new `builder.task_state.update` tool. This wasn't specified
  in the original doc — I made this split so the model can't fabricate validation state
  inside TaskState, consistent with the existing "no claiming success from file writes
  alone" rule already in the system prompt. Worth a second opinion.
- `BuilderExecutionService.__init__` now constructs `self.task_state = TaskStateService(store)`.
- `_system_prompt()` rewritten: full 14-point engineering contract, frontend visual
  checklist, scope-aware framing (`targeted_change` vs `substantial_change`), injected
  TaskState summary. Old "polished, responsive, interactive application" generic line removed.
- `_compact_messages()` replaced with `_compact_engineering_context()` (async now — call
  sites updated to `await`): pins system contract + TaskState + original request, keeps last
  `BUILDER_RAW_TAIL_MESSAGES` (12) raw, drops repetitive successful receipts via
  `_drop_redundant()`, summarizes the rest via `_summarize_engineering_history()` (separate
  from XV12's existing chat rolling summary in `context.py` — Builder-specific prompt).
- `_run()`: computes `change_scope` via `classify_change_scope()` at the top; on a
  continuation (`has_existing_workspace`), loads and carries forward the **parent session's**
  TaskState via `merge_task_state()` instead of starting fresh — this is what's supposed to
  satisfy benchmark class F (follow-up modification retains architecture/intent). Not tested.
- `_execute_tool()`: `builder.code.search`/`builder.code.map` added to the `workspace_tools`
  auto-injection set; `builder.task_state.update` gets `session_id` force-injected
  server-side (the model is never told its own session ID — this is deliberate, mirrors how
  `workspace_id`/`preview_id` are already enforced server-side elsewhere in this function).

**`app/creator.py`**
- `import fnmatch` added; `from .builder_execution import ... parse_task_state` added to the
  existing import.
- `CreatorStore.initialize()`: guarded `ALTER TABLE builder_execution_sessions ADD COLUMN
  task_state_json TEXT NOT NULL DEFAULT ''` — same pattern as the existing `access_token`
  migration on `creator_previews`, right below it.
- `CreatorStore.update_builder_session()`: `task_state_json` added to the `allowed` field set.
- `CreatorStore.builder_session_public()`: now includes `"task_state": parse_task_state(...)`.
- `WorkspaceService.code_search()` / `code_map()`: new methods, plain bounded `Path.rglob()`
  scans (no ripgrep subprocess — matches how `inspect()`/`archive()` already walk the tree
  in-process). Both have class-level constant sets for ignored dirs / source suffixes /
  config-file names / entry-point names, all with hard result-count and file-size caps.
- `CreatorPlatform.update_task_state()`: new wrapper method, delegates to
  `self.builder_execution.task_state.update()`.
- `CreatorPlatform.register()`: registers `builder.task_state.update`, `builder.code.search`,
  `builder.code.map` handlers.

**Not touched**: `app/database.py`, `app/context.py` (read for reference — the rolling
summary pattern there is what `_summarize_engineering_history` is modeled on, not reused
verbatim, per the original doc's instruction).

## 2. Open finding you should resolve first in Phase 2

**`GitService` does not `git init` workspaces.** `CreatorStore.create_workspace()` just does
`path.mkdir()` + a DB row — no `git init`. `GitService._run()` shells out to
`git -C <workspace_root> <argv>` unconditionally. On a workspace that's never been
`git init`'d (i.e., most Builder workspaces, since nothing in the current loop calls
`git.commit` proactively), `git.status`/`git.diff` will return `execution_error` with git's
"not a git repository" on stderr.

This matters because doc section 6 says quality critique needs "diff review before final
acceptance." Don't build the critique's diff-review step assuming `git.diff` always works.
Two options, pick one and note the choice in TaskState's `constraints` or your own commit
message:
1. Have the Builder loop `git init` + initial commit once per workspace on first use, then
   `git.diff` becomes real and reliable. More faithful to "diff review" as literally
   described, but changes workspace lifecycle behavior (touches `_run()` and/or
   `create_builder_session`/`create_workspace`) — bigger blast radius.
2. Skip real git diffs; build the critique's "diff" context from TaskState's deterministic
   `changed_files` list + `builder.files.read` on each changed file. Smaller, safer, doesn't
   touch workspace lifecycle. Verify this still satisfies "inspect your resulting diff" from
   the engineering contract (point 9) — it's a content diff, not a `git diff`, but it's real.

I'd lean option 2 given "preserve the known-good architecture" and "avoid band-aid fixes but
also avoid unnecessary blast radius" — but this is a judgment call, not settled.

## 3. Phase 2 — Review Intelligence (not started)

Per the original doc, section 6 + implementation order items 7–10:

1. **Quality/architecture critique** — new async method, same shape as the existing
   `_review_requirements()` static method in `builder_execution.py` (around line 900): a
   bounded `model.complete()` call with a strict-JSON-only system prompt. Doc's required
   output shape:
   ```json
   {"acceptable": false, "issues": [{"type": "...", "severity": "...", "finding": "...", "recommended_repair": "..."}]}
   ```
   Checks per doc: solved the actual request, architectural coherence, duplication,
   band-aids, unrelated-behavior preservation, test adequacy, magnitude of change matched
   the request, unnecessary complexity, unresolved regressions, and — for UI work — whether
   the requested visual characteristics were actually met (this is where Phase 3's telemetry
   becomes an input to Phase 2's critique, see below).

2. **Diff review before final acceptance** — resolve the GitService finding above first,
   then feed the diff/changed-file content into the critique prompt.

3. **Wire into the loop**: call this critique in `_run()` right after `_review_requirements()`
   succeeds (i.e., replace the direct `break` I left at the end of that block — see
   `builder_execution.py`, the `task_state = self.task_state.save(...); break` lines right
   after the `if not satisfied:` block). A failed critique should behave like a failed
   `_review_requirements()`: append a repair-instruction message, `continue` the round loop
   instead of breaking, increment `evidence.repair_cycles` if appropriate (check whether
   `_observe`'s repair-cycle bump logic already covers this path or needs a manual increment
   here — I didn't verify this).

4. **Feed critique into TaskState** — `latest_critique` field already exists in the schema
   and is already wired into `summarize_task_state()`'s prompt injection. Just
   `merge_task_state(task_state, {"latest_critique": issues, ...})` after each critique call,
   same pattern already used for the `_review_requirements` failure path (see that block for
   the exact pattern to copy).

5. **Regression-validation logic** — doc says "improve" this; current mechanism is
   `BuilderEvidence._observe()`'s repair-cycle tracking (files_changed invalidates
   sandbox/browser evidence). Read that method fully before touching it — it's small,
   already correct for the basic case, and three existing tests depend on its exact behavior
   (`test_file_changes_invalidate_prior_test_and_browser_evidence`,
   `test_chat_uses_one_durable_builder_call_and_model_repairs_negative_control`'s
   `repair_cycles == 1` assertion, `test_builder_hard_bound_returns_persisted_partial_success`).
   "Improve" probably means: also fail evidence when the *new* quality critique fails, not
   just when files change — decide whether a failed critique should reset
   `sandbox_succeeded`/`browser_healthy` the same way a file patch does, or whether it's a
   separate gate. I'd lean separate gate (critique failure ≠ technical regression) but this
   changes what "regression" means in this codebase, so think it through against the
   existing tests before committing.

## 4. Phase 3 — Frontend Intelligence (not started)

1. **Extend Chromium telemetry** — target is `BrowserService._collect_devtools()` in
   `app/creator.py` (around line 753 as of Phase 1; grep for it, line numbers have shifted
   since my edits added ~180 lines above it). This is a JS expression evaluated inside
   Chromium via DevTools Protocol — read the whole method before touching it, it's dense.
   Doc wants, per visible element: `getBoundingClientRect()`, computed background
   color/alpha, `backdrop-filter`, border, border-radius, box-shadow, font family/size/
   weight, line-height, text color, padding, margin, flex/grid values, gap, visibility,
   overflow, z-index, viewport position/overflow, overlap indicators. Return shape example
   is in the original doc section 7 — compact structured JSON per selector.
   **Be conservative and fail-soft here** — this is the riskiest piece to write blind
   (no Chromium available to test against in Cowork; you should have real Chromium via the
   sandbox once it's fixed, or via the existing headless Chromium subprocess this method
   already spawns). Make it strictly additive: existing DOM/console/network inspection must
   keep working even if the new telemetry JS throws.
2. **Inject into Builder before acceptance** — pass telemetry output into the quality
   critique's prompt (Phase 2 item 1) for UI-relevant requests, so the critique can reason
   about actual computed styles vs. what was requested, per the doc's worked example
   ("alpha is 0.92 and blur is only 4px" vs. requested translucent glass cards).
3. **Bounded visual repair cycles** — likely reuses the existing `repair_cycles`/
   `REPAIR_CYCLE_LIMIT` machinery rather than a new counter; check whether a visual-only
   repair should count against the same budget as test/browser repairs or needs its own
   bounded allowance. Doc doesn't mandate a separate counter — avoid adding one unless the
   shared budget proves insufficient in testing.
4. **Vision-critic benchmarking** (doc section 8) — explicitly a "later, only if evidence
   shows telemetry is insufficient" item. Don't build it in this pass.

## 5. Still not done after Phase 2/3

Phase 4 (context-size benchmarking 32K vs 64K, generation-limit tuning, sampling changes,
model/quantization reconsideration) — requires real hardware benchmarking, not code changes.
Comes last per the original doc's explicit ordering, only after 1–3 are verified.

## 6. Ground rules carried over (unchanged)

Don't change the model, quantization, or production context size. Don't touch Docker
isolation, capability-gateway authorization, workspace-only mounts, or network-disabled
defaults. Don't add a vision model yet. Don't build a semantic-index/LSP platform. Benchmark
before/after with the same model — a baseline capture (if one doesn't already exist from
before Phase 1 started) should happen before Phase 2/3 changes compound the diff further.
