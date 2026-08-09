# XV12 capability platform

The XODUZ conversation core is frozen at `xv12-core-frozen-v1`. Capabilities attach only through the versioned registry, live server-side authorization, execution gateway, and structured result boundary.

Normal-user grants are stored separately from conversation data and are evaluated for every invocation. Role policy is the ceiling; a grant cannot expand it. Otis, the sole administrator, has implicit registry access. The Admin settings interface is generated from the registry family/scope catalog and contains no capability-specific family list.

Local Files uses configured read roots and user-isolated managed write roots. It never exposes a shell. ADAS SI treats `X:\ADAS SI` as authoritative, stores rebuildable extraction cache under XV12 data, and restricts mutations to `_xv12_managed`; OEM PDFs are immutable. Calibration IQ uses the independent service's versioned tool API, optimistic version checks, and upstream idempotency receipts.

Capabilities return one of the normalized gateway statuses declared in the platform manifest. Argument errors, timeouts, and unhandled exceptions are isolated at the gateway and cannot terminate chat or another capability.
