# Known-good freeze acceptance evidence

Executed 2026-08-09 through the running production service and browser.

## Production conversation and capabilities

| Check | Result | Evidence |
|---|---|---|
| Natural conversation | PASS | `Good morning X.` returned a natural streamed model response |
| Identity | PASS | `Who am I?` recognized Otis as administrator |
| Continuity | PASS | Project Northstar and its calm mood were retained over four streamed turns |
| Persistence | PASS | production messages persisted with complete status |
| Auth isolation | PASS | User B received 404 for User A's private conversation; revoked session returned 401 |
| Sole admin | PASS | health reported one administrator |
| Capability permissions | PASS | admin/user role ceilings and per-user grants passed focused packs |
| Web | PASS | live provider returned timestamped current results and sources |
| Calibration IQ read | PASS | production conversation returned verified count 166 through the authenticated API |
| Offline service start | PASS | allowlisted action executed, exit code 0, domain started, HTTP 200 |
| ADAS knowledge | PASS | verified 2023 Hyundai Palisade camera procedure synthesized from receipted data |
| ADAS SI Audi artifact | PASS | `adas.si.search` returned original source pages 290-298, not the full 363-page manual |
| Exact page follow-up | PASS | `artifact.recent.read` returned page 295 without a new broad search |

## Browser/UI

- Body and app shell stayed exactly 720 px high in a 1280 by 720 viewport.
- Chat viewport owned overflow: 558 px client height with more than 2300 px of content.
- Composer bottom remained at the viewport bottom (720 px).
- Avatar top remained fixed at 268 px.
- During a live current-information stream, manual upward scroll stayed at 0 while content height grew from 3273 to 3578 px; Jump to latest remained available.
- Audi procedure card showed `ADAS SI - Pages 290-298 - Lane Change Assistance - Calibration`.
- View reference and scoped iframe were present.
- Download Section produced a browser download event.
- Copy Text wrote 14,234 characters containing calibration text.
- Print Section/Page controls targeted the same scoped PDF URL; no physical printer dispatch was performed.
- Full Document remained an optional explicit link.
- Exact page 295 produced one distinct page card alongside the procedure card; stable display keys contained no duplicates.
- Browser console contained zero errors.
- Selected TTS voice was Google US English, volume 75, unmuted; speech output was observed.
- Native STT was unavailable in the acceptance browser because microphone access was denied.

## Regressions

- Full suite: 52 passed, 0 failed, 0 skipped in 18.78 seconds.
- x-core: 17 passed in 2.70 seconds.
- chat-core: 5 passed in 0.91 seconds.
- auth: 3 passed in 0.29 seconds.
- authorization: 3 passed in 0.56 seconds.
- permissions/admin-capabilities: 3 passed each.
- session: 2 passed; memory-isolation: 4 passed; context: 2 passed.
- model-runtime: 1 passed; launcher: 2 passed.
- registry-gateway: 3 passed; registry: 7 passed; gateway: 3 passed.
- ui-shell: 2 passed; user-identity: 1 passed.
- voice: 1 passed; voice-output/TTS/voice-settings: 5 passed each; STT: 1 passed.
- project-context: 1 passed; capability-registry: 7 passed.
- web, files, Calibration IQ, standalone, databases, attachments: 1 passed each.
- ADAS: 2 passed in 12.08 seconds.
- artifacts: 10 passed in 0.89 seconds.

No pack reported a failure or skip. The freeze corrected two stale evidence assertions to the already-shipped normalized registry/status contracts; application behavior was not changed.

## Standalone audit

PASS: 76 eligible files scanned, zero XV11/BB1 donor-runtime references, and zero configured runtime paths outside XV12. Authorized independent data/services are documented separately.
