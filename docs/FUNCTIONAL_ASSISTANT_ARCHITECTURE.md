# Functional assistant architecture

The protected conversation path remains one model streaming call for ordinary chat. `AssistantOrchestrator` adds a maximum four-round native function loop only when Qwen selects a registry-supplied capability. It does not classify intent, parse tool-shaped text, or rewrite the model response.

`config/capabilities.v1.json` is the sole capability catalog. The model receives OpenAI-compatible function schemas generated from that registry; the UI and direct capability API read the same authorization metadata. `CapabilityGateway` performs the final role/risk decision before any handler runs.

Web results are bounded metadata records from live providers. ADAS queries open the XV12-owned SQLite database in read-only mode and return active verified records with citations. Calibration IQ queries use its independent authenticated read-only API. A database miss is a structured `no_result`, so the model can select another permitted source in a later bounded round.

Trusted identity and project context are structured session state. Google `sub` remains the immutable authentication key; `preferred_name` is conversational only, with the sole administrator fixed to Otis. Projects are private to their internal user owner and only one optional active project is injected into context.

The application shell is viewport-bounded. The message region alone scrolls; the avatar and composer occupy fixed flex/grid regions. Capability evidence persists on assistant messages and renders as compact inline cards. Dictation uses one browser speech-recognition session per click, exposes listening/cancel/error states, writes interim/final transcription into the ordinary composer, and never restarts itself from `onend`.

Spoken output is a separate browser speech-synthesis path. The client enumerates runtime voices, selects exact `Google US English` when present, otherwise records the effective `en-US` fallback while preserving the preferred name, and submits only completed visible assistant text for speech. The schema-v3 `voice_settings` table is the single user-scoped authority for `voice_name`, standardized 0-100 `voice_volume`, and `voice_muted`; Settings, quick mute, and the `settings.voice.update` capability all use it. Mute cancels current speech without changing volume or dictation, and synthesis errors remain non-blocking.
