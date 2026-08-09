# XV12 known-good baseline

Baseline name: `xv12-known-good-2026-08-09`

This is the recovery parent for further XV12 development. The exact Git SHA, bundle hash, state-snapshot hashes, and external backup path are recorded in the backup copy of `KNOWN_GOOD_BASELINE.json` beside the verified Git bundle.

## What works

- Local model-first XODUZ conversation with token streaming and persisted continuity.
- Authenticated conversational identity: the sole administrator is addressed as Otis.
- User-scoped conversations, summaries, projects, settings, attachments, and capability grants.
- Bounded desktop shell with chat-owned scrolling, anchored composer, fixed avatar panel, and smart scroll retention.
- Chat-native artifact cards with scoped View, Download, Print, Copy Text, and optional Full Document actions.
- ADAS knowledge lookup and direct ADAS SI OEM-PDF retrieval from `X:\ADAS SI`.
- Exact 2018 Audi A5 lane-change-assist procedure display as source pages 290-298, with exact-page follow-up reuse.
- Live web search, authenticated Calibration IQ reads, and the admin-only allowlisted Calibration IQ start action.
- User-scoped project context and voice-output settings.
- Browser speech synthesis using the selected Google US English voice. Browser speech recognition depends on microphone permission.
- Normal Windows launch through `Launch-XODUZ.cmd` and desktop shortcut `X12`.

## Frozen architecture

The X core owns model-first conversation, identity, context assembly, memory coordination, native capability selection, bounded synthesis, streaming, persistence, rendering metadata, auth/session integration, and calls through the execution gateway.

The permanent boundary is:

> XODUZ is the stable AI platform. Specialized functionality remains in independent capabilities/modules/plugins/apps connected through the versioned capability registry and execution gateway.

Capability metadata is authoritative in `config/capabilities.v1.json`. Role ceilings and user grants are evaluated server-side for every call. Capability results are structured receipts; a prepared or failed action cannot be narrated as executed.

See `docs/KNOWN_GOOD_ARCHITECTURE.md` for the exact capability inventory, external sources, model/runtime fingerprint, identity contract, persistence layout, and limitations.

## External authoritative sources

- ADAS SI OEM library: `X:\ADAS SI` (read-only originals; XV12 managed annotations are isolated under `_xv12_managed`).
- Calibration IQ: independently operated authenticated service at the configured `XV12_CALIBRATION_IQ_BASE_URL` shape.
- Web: Bing News RSS and DuckDuckGo HTML providers.

The executable standalone audit reports zero historical-project runtime dependencies; the named donor-project proof is recorded under `docs/KNOWN_GOOD_ARCHITECTURE.md`.

## Known limitations

- The current runtime is intentionally in controlled local test-auth mode. Google production OIDC requires external secret configuration.
- Native STT was not available in the freeze browser because microphone access was denied. Typed chat and TTS remained operational.
- Browser TTS uses the closest installed voice if the selected voice is not present on a particular Windows profile.
- Calibration IQ write/modify calls were not repeated during this freeze because the assignment authorized only a safe read; their authorization, version, and receipt contracts remain regression-tested.
- ADAS SI OEM originals and the independent Calibration IQ datastore follow their own data-backup policies and are not duplicated into the Git repository.

## Launch and health

1. Double-click `Launch-XODUZ.cmd` or the desktop `X12` shortcut.
2. Run `scripts\status-xv12.ps1`.
3. Confirm backend/UI port 8120 and model port 8121 are healthy.
4. Confirm alias `xoduz-qwen3-coder-30b` and context 32768.

## Restore

Follow `docs/RESTORE_KNOWN_GOOD.md`. The external backup is self-describing through `KNOWN_GOOD_BASELINE.json` and contains the verified Git bundle, source snapshot, persistent-state snapshots, model/runtime assets, evidence, and hash inventory.

## Frozen contracts

Do not casually change the identity prompt, context order, model alias/path/hash, context size, auth isolation, gateway truth boundary, registry scope vocabulary, chat rendering contract, scoped artifact behavior, ADAS source authority, launcher ownership checks, or bounded UI layout. New work starts from this tag and must pass x-core, UI, auth/isolation, launcher, existing capability packs, and its own new regression pack before a later freeze.
