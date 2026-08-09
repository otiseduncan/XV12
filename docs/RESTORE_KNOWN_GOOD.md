# Restore the XV12 known-good baseline

Use the external baseline directory named in its `KNOWN_GOOD_BASELINE.json`. Do not use XV11 or BB1 as a donor.

## 1. Verify the backup

1. Compare the Git bundle, model, llama.cpp, database snapshots, and critical-file hashes with `KNOWN_GOOD_BASELINE.json` and `SHA256SUMS.txt`.
2. Run `git bundle verify <bundle-path>` and `git bundle list-heads <bundle-path>`.
3. Confirm the baseline tag resolves to the manifest SHA.

## 2. Restore the repository

For a new checkout:

```powershell
git clone <bundle-path> X:\XV12-restored
Set-Location X:\XV12-restored
git checkout xv12-known-good-2026-08-09
```

The source snapshot in the backup is an additional recovery copy. The verified bundle is the authoritative Git-history restore input.

## 3. Restore persistent state

1. Keep XV12 stopped.
2. Copy each `.sqlite` snapshot from `persistent-state` to the exact destination mapping in the backup manifest.
3. Restore non-database state from `persistent-files` to its recorded relative path.
4. Do not restore `-wal` or `-shm` files.
5. Run `PRAGMA integrity_check` on every restored SQLite database before launch.

The snapshot contains account records and application-private state. Protect the backup with the same access controls as the live computer.

## 4. Restore model and runtime

1. Restore `models/qwen3-coder-30b-a3b/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf`.
2. Restore `runtime/llama.cpp` including the executable and required DLLs.
3. Verify the model SHA-256 is `fadc3e5f8d42bf7e894a785b05082e47daee4df26680389817e2093056f088ad`.
4. Verify `llama-server.exe` SHA-256 is `5e20cae92cdf2721d37b1d5722c4f9463e11dc643f747f72912cd83971015ec8` and version is 9906 (`33ca0dcb9`).
5. Do not change alias, context, GPU-layer, or parallel settings during recovery.

## 5. Configure secrets externally

Create an untracked `config/.env.local` from `.env.example`. Restore required secret values from the operator's secret manager, never from Git or this manifest. For Google production OIDC, configure client ID, client secret, redirect URI, immutable owner `sub`, secure-cookie policy, and `XV12_AUTH_MODE=google`.

## 6. Verify external resources

- Confirm `X:\ADAS SI` is accessible and independently intact.
- Confirm the configured Calibration IQ endpoint and its own authentication are available.
- Confirm network access if live web search is expected.
- Do not copy historical runtime code, executables, or models from XV11 or BB1.

## 7. Bootstrap and launch

If the bundled Python runtime is absent, run `scripts\bootstrap.ps1`. Then double-click `Launch-XODUZ.cmd` or run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-xv12.ps1
```

Verify with `scripts\status-xv12.ps1` and `http://127.0.0.1:8120/api/health`.

## 8. Smoke validation

1. Run `scripts\run-regression.ps1 -Pack x-core`.
2. Run `scripts\run-regression.ps1 -Pack ui-shell`.
3. Run `scripts\run-regression.ps1 -Pack auth` and `-Pack memory-isolation`.
4. Run `scripts\run-regression.ps1 -Pack artifacts` and `-Pack adas`.
5. Run `scripts\acceptance.ps1` while services are running.
6. In the UI, ask `Good morning X.` and confirm a natural streamed response.
7. Ask `Who am I?` and confirm Otis.
8. Ask for the 2018 Audi A5 lane-change-assist calibration procedure, confirm pages 290-298, then ask `Display page 295.` and confirm reuse.

Leave XV12 running only after model alias, 32768 context, backend health, scoped artifact display, and isolation checks are green.
