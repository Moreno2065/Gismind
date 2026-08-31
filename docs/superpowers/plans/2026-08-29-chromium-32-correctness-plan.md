# Chromium 32-Case Correctness Matrix Implementation Plan

> **For agentic workers:** execute inline in this session because the user explicitly requested implementation and no delegation was requested. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand stable real-browser coverage from 8 to exactly 32 Playwright Chromium tests without mocking browser HTTP, SSE, production routes, Redis, SQLite, Dispatcher, or GIS tools.

**Architecture:** Keep the existing deterministic LLM seam in `backend/scripts/e2e_awaiting_server.py`. Add marker-scoped deterministic plans only for browser fixtures, then make Chromium upload data through the real `/api/upload` endpoint and assert UI state created from the real `/api/chat` SSE stream. Keep external AMap coverage in the existing POI test; use local vector/raster fixtures for the added correctness cases.

**Tech Stack:** Playwright Chromium, React 18, Vite proxy, FastAPI, Redis, SQLite checkpoints, Rasterio, GeoPandas.

---

### Coverage Matrix

| Area | Existing | Added | Total |
|---|---:|---:|---:|
| Chat/SSE lifecycle and error convergence | 2 | 6 | 8 |
| Upload identity, order, rejection and expiration | 3 | 7 | 10 |
| GIS vector/raster semantics and map rendering | 2 | 6 | 8 |
| Awaiting-input, cancellation and session isolation | 1 | 5 | 6 |
| **Chromium tests** | **8** | **24** | **32** |

### Task 1: Add real-browser fixtures and assertions

**Files:**
- Create: `frontend/e2e/correctness-matrix.spec.ts`

- [x] Added inline helpers that upload browser `File` objects, submit chat messages, collect passive request/response evidence, and wait for visible terminal UI state.
- [x] Verified the configured Chromium count with `npx playwright test --list`: exactly 32 tests in three specs.
- [x] Ran the new spec; its first run exposed three DOM-assertion defects. The assertions were tightened to distinguish visible user-facing errors from hidden debug JSON, then the three cases passed.

### Task 2: Reuse the existing deterministic production scenarios

**Files:**
- Existing: `backend/scripts/e2e_awaiting_server.py`
- Test: `frontend/e2e/correctness-matrix.spec.ts`

- [x] Reused the existing marker-scoped deterministic plans for valid-empty, expired-upload, one/two GeoJSON, GeoTIFF slope, and awaiting-input flows; no production test-server behavior was changed.
- [x] Confirmed each browser case still uses the real Vite proxy, FastAPI public route, Dispatcher, Redis/SQLite, SSE, and registered native tool handler. Only LLM output is deterministic.

### Task 3: Add 24 independent Chromium cases

**Files:**
- Create: `frontend/e2e/correctness-matrix.spec.ts`

- [x] Chat/SSE: initial surface, whitespace guard, Shift+Enter, Enter submission, terminal answer, visible plan trace, and empty-map convergence.
- [x] Upload contract: ready metadata, exact one-file `file_id`, `data_io_read`, file removal, rejected upload, expired-file error/recovery, two-file ordering, and overlay result.
- [x] GIS/map: point vector overlay, two-polygon overlay, GeoTIFF metadata and payload identity, visible loading/slope tools, and raster `ImageLayer` rendering.
- [x] Awaiting-input: banner remains actionable after its SSE stream ends. The pre-existing eight cases retain resume, cancellation/no-late-token, session switch, and refresh isolation coverage.
- [x] The new 24-case spec passed after the assertion correction; the complete 32-case suite then passed.

### Task 4: Verify the complete stable suite

**Files:**
- Modify: `frontend/playwright.config.ts` only if timeout or reporting must cover all 32 cases.

- [x] `npx playwright test --list` reports exactly 32 Chromium tests.
- [x] Full Chromium suite result: `32 passed (6.7m)` using the workspace Python and real Redis.
- [x] `npm run build` passed; ports 18000 and 15173 were confirmed stopped. The Vite build retains its existing ECharts chunk-size warning.
