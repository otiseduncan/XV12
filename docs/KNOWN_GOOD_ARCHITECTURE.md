# XV12 known-good architecture

## Recorded pre-freeze state

- Branch: `main`
- HEAD before freeze records: `5d8fc4644211b1a4c9b382a313ef2236bc45d04b`
- Upstream: none configured
- Git remote: none configured
- Working tree: clean; zero modified, staged, or untracked files
- Backend/UI: healthy on `127.0.0.1:8120`, owned PID 277364
- Model runtime: healthy on `127.0.0.1:8121`, owned PID 288040
- Model catalog: `xoduz-qwen3-coder-30b`; context 32768
- Database schema: 3 (`0003_voice_output_settings`)
- Capability permission schema: 1
- Artifact schema: 2
- ADAS SI cache schema: 1
- Capability registry: 3.2.0 with 23 registered capabilities

The PIDs describe the untouched pre-freeze observation. They are expected to change after a normal launcher restart.

## Frozen X core

`ContextAssembler` packs trusted context in this order:

1. XODUZ identity contract
2. authenticated user identity and role
3. explicitly attached active project, when present
4. active subject state, when present
5. rolling conversation summary, when present
6. most recent conversation messages that fit the budget

The assistant gives the local model ownership of interpretation and wording. It exposes registry-generated native tools, executes selected tools through `CapabilityGateway`, returns bounded structured results to the model, and limits the tool loop to four rounds. Streaming deltas persist with truthful complete/interrupted/failed state. Hidden reasoning is not stored.

`UserScopedStore` is the privacy boundary for users, sessions, conversations, messages, summaries, projects, active subjects, settings, and attachments. Google `sub` is the production identity key; one partial unique index and owner reconciliation enforce the sole administrator. In the frozen local test mode, three fixed test identities exercise the same session and isolation boundaries.

The browser renderer treats chat as the universal display surface. Capability cards are metadata-driven. Artifact identity and display keys preserve source continuity, and PDF slices retain original source-page imagery. Full-document access is secondary and explicit.

## Capability boundary

Permanent rule:

> XODUZ is the stable AI platform. Specialized functionality remains in independent capabilities/modules/plugins/apps connected through the versioned capability registry and execution gateway.

`available` below means the handler is registered and health/contract checks passed. `dynamic` means readiness also depends on an external resource.

| Capability ID | Family | Version | Scopes | Health | Authoritative source/service | Freeze readiness |
|---|---|---:|---|---|---|---|
| `system.health.read` | system | 1.0.0 | read | available | XV12 backend/runtime state | production-ready |
| `web.current.search` | web | 1.0.0 | read | available | Bing News RSS, DuckDuckGo HTML | live read passed |
| `adas.coverage.read` | adas_si | 1.0.0 | read | available | `data/knowledge/adas_knowledge.sqlite` | production-ready |
| `adas.knowledge.search` | adas_si | 1.0.0 | read | available | verified XV12 ADAS knowledge | live read passed |
| `calibration_iq.repair_orders.read` | calibration_iq | 1.0.0 | read | dynamic/available | authenticated Calibration IQ API | live read passed |
| `files.local.read` | files | 1.0.0 | read | available | configured bounded local roots | regression passed |
| `artifact.recent.read` | artifacts | 1.0.0 | read | available | user/conversation-scoped artifact store | live browser passed |
| `files.local.write` | files | 1.0.0 | write | available | user-managed XV12 file root | regression passed |
| `files.local.modify` | files | 1.0.0 | modify | available | user-managed XV12 file root, SHA guard | regression passed |
| `adas.si.inventory.read` | adas_si | 1.0.0 | read | dynamic/available | `X:\ADAS SI` | live inventory passed |
| `adas.si.search` | adas_si | 1.0.0 | read | dynamic/available | OEM PDFs plus XV12 cache | live Audi procedure passed |
| `adas.si.record.write` | adas_si | 1.0.0 | write | available | `X:\ADAS SI\_xv12_managed` only | regression passed; OEM originals immutable |
| `adas.si.record.modify` | adas_si | 1.0.0 | modify | available | versioned XV12 managed records | regression passed; OEM originals immutable |
| `calibration_iq.repair_orders.write` | calibration_iq | 1.0.0 | admin write | dynamic/available | authenticated Calibration IQ mutation API | receipt/version contract tested; not mutated in freeze |
| `calibration_iq.repair_orders.modify` | calibration_iq | 1.0.0 | admin modify | dynamic/available | authenticated Calibration IQ mutation API | receipt/version contract tested; not mutated in freeze |
| `project.list` | projects | 1.0.0 | read | available | user-scoped XV12 database | regression passed |
| `project.register` | projects | 1.0.0 | write | available | user-scoped XV12 database | regression passed |
| `project.activate` | projects | 1.0.0 | modify | available | user-scoped XV12 database | regression passed |
| `project.detach` | projects | 1.0.0 | modify | available | user-scoped XV12 database | regression passed |
| `service.calibration_iq.start` | services | 1.0.0 | admin execute | available | fixed `X:\calibration iq` Docker Compose launcher | live offline-start passed |
| `settings.voice.read` | settings | 1.0.0 | read | available | user-scoped XV12 settings | live read passed |
| `settings.voice.update` | settings | 1.0.0 | modify | available | user-scoped XV12 settings | regression passed |
| `admin.capabilities.inspect` | administration | 1.0.0 | admin read | available | versioned capability registry | regression passed |

Role policy is the ceiling. Normal-user grants are stored in `data/capabilities/permissions.sqlite` and cannot grant an admin-only scope. Otis has administrator implicit access. Gateway status and execution receipts remain authoritative.

## External authoritative resources

### ADAS SI

- Authoritative path: `X:\ADAS SI`
- Purpose: OEM service-information PDF library for direct source retrieval
- Access: recursive PDF discovery and bounded page extraction by `adas.si.*`
- Current inventory: 77 files, 76 PDFs, 218,834,976 bytes
- Cache: `data/capabilities/adas_si/index.sqlite`, schema 1, 671 cached pages
- Managed writes: only `X:\ADAS SI\_xv12_managed`; OEM originals are immutable
- Rebuild: stop XV12, preserve or remove only the rebuildable cache file, relaunch, then call inventory/search; pages are lazily extracted and keyed by path plus source mtime
- Backup policy: the OEM library remains independently backed up and is not copied into the Git repository or this application-state snapshot

### Calibration IQ

- Endpoint shape: `XV12_CALIBRATION_IQ_BASE_URL`, default `http://127.0.0.1:8084/api/v1/tools/v1/calibration-iq`
- Capability version: 1.0.0
- Operations: bounded read for admin/user; versioned, idempotent write/modify for admin only; allowlisted service start for Otis only
- Authentication: owned by the independent service configuration; no credential is stored in the known-good manifest
- Freeze result: safe read returned 166 repair orders; offline-start action restored HTTP 200 health

### Web

- Providers: Bing News RSS and DuckDuckGo HTML
- Operation: bounded read only
- Freeze result: fresh evidence returned with execution timestamp and sources

## Model and llama.cpp freeze

- Model: Qwen3-Coder-30B-A3B-Instruct Q4_K_M
- GGUF: `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`
- Absolute path: `X:\XV12\models\qwen3-coder-30b-a3b\Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`
- Size: 18,556,689,568 bytes
- SHA-256: `fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad`
- Alias: `xoduz-qwen3-coder-30b`
- llama.cpp executable: `X:\XV12\runtime\llama.cpp\llama-server.exe`
- llama.cpp version: 9906 (`33ca0dcb9`), Clang 20.1.8, Windows x86_64
- llama.cpp SHA-256: `5e20cae92cdf2721d37b1d5722c4f9463e11dc643f747f72912cd83971015ec8`
- Context: 32768; GPU layers: 99; parallel slots: 1
- Startup arguments: `-m <GGUF> --alias xoduz-qwen3-coder-30b --host 127.0.0.1 --port 8121 -c 32768 -ngl 99 --parallel 1 --no-webui`
- Inference endpoint: `http://127.0.0.1:8121/v1`
- Health proof: `/v1/models` contains the exact alias; XV12 `/api/health` also verifies owned executable/model paths and context

## XODUZ identity freeze

- Contract symbol: `app.context.IDENTITY_CONTRACT`
- Version: `xoduz-identity-v1`
- UTF-8 SHA-256: `db78f405bd11158bce0de9eaade15883579e64df379b21536896fdd0c12eab1c`
- Length: 595 characters
- Admin conversational name: `Otis`
- Authenticated context format: conversational name, role, and trusted internal user ID; the ID is for scoping and should not be surfaced unless explicitly requested
- Context packing: identity, authenticated user, optional project, optional active subject, optional rolling summary, recent messages

## XV12-owned persistence

| Store | Schema | Freeze inventory |
|---|---:|---|
| `data/xv12.db` | 3 | users/accounts, sessions, 67 conversations, 378 messages, projects, summaries, subjects, settings, attachments, traces |
| `data/capabilities/artifacts.sqlite` | 2 | 20 artifact records plus immutable generated slices under `data/capabilities/artifact_slices` |
| `data/capabilities/permissions.sqlite` | 1 | 3 capability grants |
| `data/capabilities/adas_si/index.sqlite` | 1 | 671 cached source pages; rebuildable |
| `data/knowledge/adas_knowledge.sqlite` | migration 0001 | 76 ingested document records and verified/review workflow tables |

All five stores returned `PRAGMA integrity_check = ok` before snapshotting. Live SQLite files must be backed up through the SQLite backup API; WAL/SHM files are not restoration inputs.

## Standalone boundary

The executable standalone audit scanned production code, configuration, scripts, and documentation and returned PASS with zero donor-runtime references and zero configured runtime paths outside XV12. `X:\ADAS SI`, Calibration IQ, and web providers are authorized external data/services, not historical application-runtime dependencies.
