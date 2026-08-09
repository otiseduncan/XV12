# XV12 Creator / Builder Known-Good Freeze

Baseline: `xv12-known-good-creator-builder-artifact-v1`

Frozen from proven source SHA:

`8140e59fd8ed22bd8cdf377f1fbea3579aa92634`

This is an additive known-good milestone. It does not move or replace any earlier XV12 frozen-core, capability-platform, or Creator tags/baselines.

## Proven at this point

- Durable model-directed Builder execution sessions operate outside the ordinary short chat tool-round limit.
- Builder workspaces persist across follow-up modifications.
- Only one active Builder job is allowed for a conversation/workspace; duplicate same-turn status behavior is deduplicated rather than spawning a competing job/card.
- Generated conversation image artifacts can be staged into the active Builder workspace as real local assets.
- When a request depends on a staged conversation asset, Builder acceptance requires evidence that project source actually references that asset before success may be reported.
- Completed applications are tested, browser-validated, and returned as live application artifacts in X chat.
- Project archive/download artifacts remain available.
- ComfyUI-backed photorealistic image generation is available as the realistic image provider.
- The protected XODUZ conversation/model core remains preserved under `xv12-core-frozen-v1`.

## Regression evidence

GitHub Actions workflow: `XV12 capability contract`

Run: `31339488340`

Result: **PASS**

The portable gate includes the permanent regressions for:

- Builder execution sessions
- active Builder job deduplication
- Builder conversation-artifact handoff and source-reference verification
- existing frozen-core/capability/auth/artifact/memory/model/voice suites

## Recovery rule

Treat this milestone as known good for Creator/Builder work.

Future Creator, generative-video, Builder, media, or deployment changes should follow:

> Build -> Prove -> Freeze -> Extend

If a later change regresses Builder continuity, artifact handoff, live chat rendering, or X's frozen core, return to this known-good point rather than repairing forward from a red baseline.

## Scope boundary

This freeze intentionally covers the current Creator/Builder/artifact-handoff state. Real generative image-to-video work is a later milestone and must earn its own test/proof/freeze before being considered known good.
