# Functional assistant architecture

The protected conversation path remains one model streaming call for ordinary chat. `AssistantOrchestrator` adds a maximum four-round native function loop only when Qwen selects a registry-supplied capability. It does not classify intent, parse tool-shaped text, or rewrite the model response.

`config/capabilities.v1.json` is the sole capability catalog. The model receives OpenAI-compatible function schemas generated from that registry; the UI and direct capability API read the same authorization metadata. `CapabilityGateway` performs the final role/risk decision before any handler runs.

Web results are bounded metadata records from live providers. ADAS queries open the XV12-owned SQLite database in read-only mode and return active verified records with citations. Calibration IQ queries use its independent authenticated read-only API. A database miss is a structured `no_result`, so the model can select another permitted source in a later bounded round.

Trusted identity and project context are structured session state. Google `sub` remains the immutable authentication key; `preferred_name` is conversational only, with the sole administrator fixed to Otis. Projects are private to their internal user owner and only one optional active project is injected into context.

The application shell is viewport-bounded. The message region alone scrolls; the avatar and composer occupy fixed flex/grid regions. Capability evidence persists on assistant messages and renders as compact inline cards. Voice uses one browser speech-recognition session per click, exposes listening/cancel/error states, writes interim/final transcription into the ordinary composer, and never restarts itself from `onend`.
