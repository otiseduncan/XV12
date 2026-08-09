# Baseline 1 architecture

XV12 keeps the model boundary deliberately narrow. `LlamaModel` is the only OpenAI-compatible model client. `ContextAssembler` creates the ordered prompt envelope, while response content streams back unchanged and persists with `complete`, `interrupted`, or `failed` truth.

`UserScopedStore` is the private-data access boundary. Its method signatures make internal `user_id` mandatory for reads and writes. SQLite foreign keys and composite query filters provide defense in depth.

Authentication is split from authorization. Google `sub` determines identity; the configured owner `sub` alone maps to `admin`. A partial unique database index and owner reconciliation enforce one administrator. `CapabilityGateway` checks role and risk before a registered handler executes; an approval concept cannot bypass that decision.

The Windows launcher records process identity under `runtime/state`. Health is not a port-open check: the model catalog must report the configured alias, the launch state must point to this repository root, and the stored context contract must match 32,768 tokens.

The UI is a static application served by the backend, so normal startup has two long-running components rather than a disposable development server: llama-server and the XV12 backend/UI host.
