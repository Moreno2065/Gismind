# Gismind P0 Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. This session executes inline because the user asked to start fixing and no delegation was requested.

**Goal:** Make Root-Planner provenance, live GIS semantic correctness, browser map rendering, and stream isolation fail closed under repeatable tests.

**Architecture:** Keep Dispatcher as the only orchestrator. Strengthen the black-box contract around its public `run.plan` and tool events, add result metadata only at the producing tool boundary, and isolate concurrent streams by run identity without changing DAG semantics.

**Tech Stack:** Python 3.13, pytest, FastAPI, LangGraph, Rasterio, React 18, TypeScript, Playwright Chromium.

---

### Task 1: Root-Planner evaluation provenance

**Files:**
- Create: `backend/tests/unit/test_root_planner_synonym_contract.py`
- Modify: `blackbox/root_planner_synonym_cases.json`

- [x] Add a failing test that loads the authored cases and permits `guardrail` only for the exact coordinate-conversion safety case RP02.
- [x] Run the focused contract and observe RP03/RP05 fail while still declared as guardrails.
- [x] Remove the overrides and narrow production `_strong_constraint_guardrail_plan` to the coordinate safety case.
- [x] Verify valid Root DAGs report `root_llm` and invalid Root DAGs use an explicit `fallback`.

### Task 2: Live semantic assertions

**Files:**
- Modify: `backend/tests/unit/test_root_planner_synonym_contract.py`
- Modify: `blackbox/root_planner_synonym_suite.py`

- [x] Add failing tests for result normalization, RP03 exact station records, RP04 intersection geometry/area, RP05 class metadata, and RP07 buffered/exported feature counts.
- [x] Run the focused tests and verify each fails because the semantic checker is absent or incomplete.
- [x] Add small result-decoding and GeoJSON measurement helpers to the black-box runner.
- [x] Make `_validate` invoke the authored semantic checker and record semantic metrics in evidence.
- [x] Re-run focused tests; expect PASS.

### Task 3: Raster classification evidence

**Files:**
- Modify: `backend/tests/unit/test_raster_analysis.py`
- Modify: `backend/app/tools/raster_analysis.py`

- [x] Add a failing assertion that reclassification returns class counts for values 1, 2, and 3.
- [x] Run the focused raster test; observe failure because `class_counts` is missing.
- [x] Compute counts from valid output pixels and return them with the raster result.
- [x] Re-run focused raster tests; expect PASS.

### Task 4: POI transport versus valid-empty behavior

**Files:**
- Modify: `backend/tests/unit/test_poi_query.py`
- Modify: `backend/app/tools/poi_query.py`
- Modify: `backend/app/agents/dispatcher.py`

- [x] Reproduce the RP06 second-turn failure and capture Amap/OSM terminal reasons.
- [x] Add failing tests for valid AMap zero results, OSM outages, and both-provider outages.
- [x] Preserve provider status and successful zero-result artifacts; compare zero against prior-session counts.
- [x] Run POI and Dispatcher focused suites (65 passed); live public-API rerun remains in Task 7.

### Task 5: Browser map render proof

**Files:**
- Modify: `frontend/src/components/LazyMapView.tsx`
- Modify: `frontend/src/components/RasterOverlay.tsx`
- Modify: `frontend/src/lib/mapRenderers.ts`
- Modify: `frontend/e2e/stable-wiring.spec.ts`

- [x] Add failing Playwright assertions for map-ready state and actual vector/raster overlay counts.
- [x] Expose data attributes driven by overlay instances returned/attached by AMap.
- [x] Replace invalid AMap v2 `ImageOverlay` usage with the runtime-supported `ImageLayer` in inline and fullscreen paths.
- [x] Verify real AMap POI markers, GeoJSON point, two-file polygon overlay, and GeoTIFF slope ImageLayer in Chromium.

### Task 6: Stream isolation and backpressure

**Files:**
- Modify: `backend/tests/integration/test_sse_events.py`
- Modify: `backend/app/api/chat.py`
- Modify: `backend/app/agents/events/__init__.py`

- [x] Add failing tests for old/new collectors in one session and bounded queue overflow behavior.
- [x] Make collector unregister identity-safe so an old run cannot remove a newer same-session registration.
- [x] Bound the event queue at 512 and record deterministic dropped-event counts while preserving latest events and the stop sentinel.
- [x] Run collector/SSE suites (26 passed); Chromium switch/stop remains in Task 7.

### Task 7: Verification evidence

**Files:**
- Create result files under `blackbox-results/` only when running live services.

- [x] Run focused backend regressions after every task; final backend result: 1463 passed, 2 skipped.
- [x] Run frontend typecheck/build and the full Chromium suite; final browser result: 8 passed.
- [x] Run Root-Planner RP01–RP07 with real LLM/services and record provenance, semantics, SSE, terminal tools, answer, map count, and reasons in `blackbox-results/ROOT_PLANNER_SYNONYM_20260828_FIX_RUN5_FINAL.json` (7/7).
- [x] Repeat root-LLM cases three times in cold sessions; final repeat evidence is `blackbox-results/ROOT_PLANNER_SYNONYM_20260828_FIX_RUN6_ROOT_REPEAT.json` (6/6), combined with Run 3/4/5 per-case evidence.
- [x] Stop all test services and verify ports 18001/18000/15173/5173 are no longer listening.

Final RP06 semantic regression after pruning the redundant previous-brand
lookup: `blackbox-results/ROOT_PLANNER_SYNONYM_20260829_FIX_RUN8_RP06_SEMANTIC.json`
(`root_llm`, previous=20, current=20, map features=20, passed).
