# Chromium 32-Case Stable Coupling Result — verified 2026-08-31

## Result

- Playwright Chromium discovery: `32 tests in 3 files`.
- Latest isolated full run on the final code: `32 passed (1.8m)`.
- Machine-readable JUnit result: `blackbox-results/CHROMIUM_32_FINAL_20260831.xml`.
- The `blackbox-results` report is a local generated artifact and is intentionally ignored by Git; rerun the command below to recreate it.
- Frontend production build: passed (`tsc -b && vite build`).
- After the run, neither test server port was listening: `18000` (FastAPI) and `15173` (Vite).
- This document is the durable summary for the final run. The earlier captured raw baseline remains at `blackbox-results/CHROMIUM32_20260830_R3.stdout.log`.

## What ran

Every case used Chromium to send a real request through the Vite `/api` proxy to FastAPI. The test server used real public upload/chat routes, Redis database 15, SQLite checkpoints, Dispatcher, registered native tools, SSE parsing, React state, trace rendering, and map-layer rendering. Request and response observation in the tests was passive; there were no mocked or intercepted HTTP/SSE responses.

The LLM seam was deterministic, scoped to the test server's existing marker prompts. Therefore this result is stable full-stack coupling evidence, not a claim that the live Root LLM understood these prompts. The POI case does call the configured real AMap service and asserts an AMap-sourced, rendered non-empty layer; live Root-LLM semantic evaluation remains a separate suite.

The 32 cases cover text POI/SSE/trace/map, single and two-file GeoJSON identity/order/overlay, GeoTIFF slope/raster layer, empty/error/expired-upload states, awaiting-input/resume, stop/no-late-token, session switch, and refresh recovery.

During the final verification cycle the suite first exposed an expired upload being reported as a factual empty result. The backend now returns `UPLOAD_EXPIRED` as an error, and the focused 2-case regression plus the complete 32-case rerun passed. The same run also exposed harmless but noisy concurrent cleanup races on Windows; transient locks are retried and a directory already removed by another cleanup worker is treated as an idempotent success.

A later repeat exposed a test-runner lifecycle fault: the Vite child could disappear between consecutive Windows Playwright runs, so one test timed out and every later navigation failed with `ERR_CONNECTION_REFUSED`. The local runner now starts FastAPI and the repository's local Vite entry itself, waits for both HTTP health checks, passes their fixed endpoints to Playwright, and stops the exact owned PIDs in `finally`. Two immediately consecutive focused runs and the following complete 32-case run all passed; both test ports were released afterward.

## Reproduce

From `frontend`, use the fail-fast local runner. It loads the frontend JavaScript key and backend AMap service key before Playwright starts Vite/FastAPI, so a missing key cannot masquerade as a map-rendering regression late in the suite:

```powershell
$frontendEnv = 'C:\path\to\frontend\.env.local'
$backendEnv = 'C:\path\to\backend\.env'
$python = 'C:\path\to\backend\.venv\Scripts\python.exe'
.\scripts\run-e2e-local.ps1 `
  -FrontendEnvPath $frontendEnv `
  -BackendEnvPath $backendEnv `
  -BackendPython $python
npm run build
```

The local Redis instance must be available with the configuration in `backend/.env`; the runner starts and stops FastAPI on `18000` and Vite on `15173`, while Playwright drives Chromium against those owned services. The runner requires non-empty `VITE_AMAP_KEY`, `VITE_AMAP_SECURITY_CODE`, and `AMAP_KEY` before it starts any test. A worktree commonly does not contain private `.env` files, so pass the original absolute env paths explicitly as shown above.

## Notes

The Vite build emitted its existing ECharts large-chunk warning. It did not fail type checking or bundle generation. Do not run `npm run build` concurrently with this Windows Playwright command: npm process-tree cleanup can terminate a test server and create cascading `ERR_CONNECTION_REFUSED` failures. Live-LLM semantic smoke tests remain separate and must record `planner_source` independently.
