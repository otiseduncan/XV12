# XV12 capability truth, entity, and enumeration contract

Status: required platform contract after `xv12-known-good-2026-08-09`.

This contract repairs the capability-layer defect exposed by the ADAS inventory conversation without changing the model-first XODUZ conversation loop.

## Governing boundary

XODUZ is the stable AI platform. Domain data, parsing, indexing, APIs, and business rules live in independent capabilities. A capability may fail, be removed, or be upgraded without changing X's natural conversation, identity, launch behavior, UI shell, memory, or unrelated capabilities.

## Truth contract

Specific authoritative records attributed to a capability must be present in that capability's returned evidence.

X may summarize, sort, explain, compare, or transform returned records. X must not invent authoritative vehicle applications, repair orders, files, users, projects, or equivalent concrete rows because only an aggregate count was returned.

Every result passing through `CapabilityGateway` receives an `evidence_contract` that states:

- authoritative records only;
- specific records must be present in the result;
- counts do not imply unreturned rows.

The model-facing capability description carries the same rule. This is evidence discipline, not deterministic answer rewriting.

## Entity semantics contract

Counts are meaningful only for the entity type explicitly named by the capability.

The following are different entities and may never be treated as interchangeable unless a contract explicitly guarantees equivalence:

- source document;
- normalized document row;
- vehicle application;
- procedure record;
- verification record;
- review-queue item;
- repair order;
- file;
- project;
- user.

For ADAS specifically, `76 documents`, `6 verification records`, and `N vehicle applications` are three different statements. Subtracting one from another does not create an inventory.

## Enumeration contract

If a capability exposes an aggregate count over user-viewable entities, the same capability family must expose a bounded way to enumerate those entities when the user asks to list/show/inspect them.

For the current ADAS SI source library, `adas.si.inventory.read` returns both:

- source-document inventory; and
- unique year/make/model applications derived from source-document identity.

The current source inventory is small enough to return in one bounded result. If a capability grows beyond a safe response size, it must add server-side pagination/reference handles rather than inject an unbounded inventory into the model context.

Future data-centric capability families should use cohesive operations equivalent to:

- `*.summary` / `*.coverage` — aggregates;
- `*.list` / `*.inventory` — bounded enumeration;
- `*.search` — query/retrieval;
- `*.get` — exact item;
- `*.section.get` — exact scoped source section when applicable.

Names may vary by domain. The semantic separation may not.

## ADAS SI authoritative source semantics

Authoritative source library: `X:\ADAS SI`.

`AdasSourceInventory` enumerates the OEM PDF source library independently of the XV12 normalized ADAS database.

Source documents are described separately from unique vehicle applications. Vehicle applications are derived conservatively from source-document identity and carry their supporting document titles/paths. Documents that cannot be parsed confidently remain in `unparsed_documents`; the system does not manufacture a year/make/model for them.

The owner's source-library verification is represented separately from ingestion/review pipeline metrics:

- source-library operator verification: owner assertion;
- normalized `verification_records`: database rows;
- review queue: workflow state.

The word `verified` must not collapse those concepts.

## Normalized ADAS database semantics

`adas.coverage.read` remains the normalized XV12 database view. Its legacy `coverage` keys are preserved for compatibility, but `coverage_summary` and `entity_semantics` define their exact meanings.

The result explicitly directs authoritative source-library inventory questions to `adas.si.inventory.read`.

## Artifact state

The existing scoped artifact contract remains unchanged:

- retrieve broadly, display narrowly;
- retain source ID + page/section scope;
- exact-page/section follow-ups reuse the existing artifact where possible;
- equivalent artifacts are deduplicated/focused rather than appended repeatedly;
- full-document access is secondary and explicit.

## Permission contract

The existing registry-driven admin permission model remains unchanged.

- Otis/admin receives implicit access subject to risk/approval policy.
- Normal-user capability/scopes come from the registry and server-side grant store.
- The model never grants access.
- Approval cannot override missing authorization.

New capabilities must register their family and supported scopes so the admin permission UI can represent them without source-code changes.

## Registration conformance

The registry now rejects duplicate capability IDs and capabilities without a valid object argument schema. A future plugin must conform to the registry contract rather than forcing special-case recovery into X core.

## Required regressions

At minimum protect these behaviors:

1. document count and vehicle-application count remain distinct;
2. multiple documents for one year/make/model collapse into one application with source provenance;
3. unparseable source documents remain explicit instead of generating invented applications;
4. every gateway result receives the generic evidence contract;
5. duplicate capability IDs are rejected;
6. the frozen X core suite remains green after all capability changes.

## Permanent development rule

Build → Prove → Freeze → Extend.

If capability work changes X's greeting, personality, identity, streaming, context continuity, launcher, permanent UI shell, memory isolation, or unrelated capabilities, stop feature work and restore the frozen core contract before continuing.
