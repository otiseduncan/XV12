# Owner data inventory and runtime disposition

This inventory records the data systems intentionally connected to X. It is not a catalog of every local database found on the workstation.

| System | Disposition | Runtime boundary | Proof |
|---|---|---|---|
| ADAS knowledge | Migrated into XV12 ownership | Read-only SQLite at `data/knowledge/adas_knowledge.sqlite` | 76 documents, 6 active verified records, 1 verified vehicle application, and cited Hyundai Palisade procedure data |
| Calibration IQ | Connected as an independent product | Authenticated read-only API at the locally configured Calibration IQ service; no direct access to its PostgreSQL store | Live count and bounded repair-order query; separate health contract |
| Gmail and calendar connectors | Not migrated as databases | Service connectors, not owner database resources for this phase | Excluded intentionally; no credential-store copying |

Donor provenance: the ADAS SQLite source was inspected in the prior XODUZ project and copied byte-for-byte into XV12-owned storage with SHA-256 `DD677DCEB5946BF78A64FB07BC2F4F9933211C9A633973115BFC5264DF7ED9A6`. Production code and configuration contain no donor path.

Calibration IQ remains independent by design. XV12 reads it only through its scoped bearer-token API. The sole Tier 2 start action has no caller-supplied command or path: it runs `docker compose up -d` in the one configured Calibration IQ project directory and returns a health receipt. Normal users cannot receive or execute it.
