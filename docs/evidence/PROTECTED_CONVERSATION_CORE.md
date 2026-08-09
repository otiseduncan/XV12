# Protected conversation core

Starting commit: `2cbaafd70eaea1c752decda79d574db293e64691`

Protected tag: `xv12-baseline-core-v1`

The production chat path was exercised before functional-assistant changes on 2026-08-09. The local model, alias, quantization, 32,768-token context, context assembler, SSE endpoint, and message persistence are protected.

Observed live behavior:

| Turn | First token | Total | Result |
|---|---:|---:|---|
| `Good morning X.` | 0.451 s | 0.839 s | Natural model-generated greeting |
| Establish Project Northstar and its calm mood | 0.319 s | 0.806 s | Subject accepted naturally |
| Ask for the prior project name and mood | 0.325 s | 0.887 s | Correct continuity: Northstar and calm |
| `Who are you?` | 0.487 s | 1.330 s | Identified herself as XODUZ / X inside XV12 |

Every turn emitted production SSE `meta`, multiple `delta`, and `done` events. The three-turn continuity conversation persisted six messages. Functional capability work must retain one model-first conversation path and may not add phrase routing or response rewriting.
