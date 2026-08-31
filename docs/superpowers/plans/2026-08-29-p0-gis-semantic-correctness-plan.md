# P0 GIS Semantic Correctness Implementation Plan

> **For agentic workers:** execute inline in this session because the user explicitly requested implementation and current collaboration rules prohibit delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a successful GIS answer provably agree with the task-scoped, semantically valid tool result and rendered map.

**Architecture:** POI providers remain sources only; `POIQuery` recomputes and enforces circular distance before deduplication. Dispatcher outcomes gain immutable production metadata from the dispatched task and returned tool payload. Assembly selects leaf/current-task artifacts by provenance instead of summing by role. A shared postcondition layer rejects semantically invalid tool successes before they become artifacts or SSE `done` results.

**Tech Stack:** FastAPI, Pydantic, LangGraph Dispatcher, GeoPandas/Rasterio, pytest, live blackbox SSE suite.

---

### Task 1: Enforce POI circular radius at the tool boundary

**Files:**
- Modify: `backend/app/tools/poi_query.py`
- Test: `backend/tests/unit/test_poi_query.py`

- [x] Write failing public-tool tests with a 500 m center, a 0 m POI, and a provider-supplied 2 km POI that falsely claims 1 m; repeat for OSM records without `distance`.
- [x] Verify RED: both tests returned the 2 km point because `search_poi_tool()` did no exact-radius check.
- [ ] Add a named meter tolerance, recompute every output distance with `haversine_m`, retain only `distance <= radius + tolerance`, then deduplicate/count/format the filtered list.
- [ ] Return the exact query, center, radius, tolerance, and CRS in the structured POI data used by Dispatcher artifacts.
- [ ] Verify GREEN: run the two radius tests plus existing POI unit tests.

### Task 2: Preserve task-scoped artifact provenance and stop POI cross-wiring

**Files:**
- Modify: `backend/app/agents/dispatcher.py`
- Test: `backend/tests/unit/test_dispatcher.py`

- [x] Write failing dispatch test asserting `task_id`, `tool_name`, exact query, input `file_id`, CRS, and dependency IDs are attached to the outcome artifact.
- [x] Write failing assembly test with an erroneous 20-result Mixue branch plus the requested 2-result Chabaidao branch; require answer, result, and map counts to all equal 2.
- [x] Verify RED: artifact had no provenance and assembly reported 22.
- [ ] Attach metadata immediately after `run_sub_agent()` returns, deriving facts from the task plus structured tool result without inferring missing file IDs or CRS.
- [ ] Select POI artifacts by current task/query provenance for a comparison; emit only the selected result/layer/count and fail the run if the requested artifact cannot be identified uniquely.
- [ ] Verify GREEN: run the new dispatcher tests and existing assembly tests.

### Task 3: Add semantic postconditions at the production tool boundary

**Files:**
- Create: `backend/app/agents/postconditions.py`
- Modify: `backend/app/agents/dispatcher.py`
- Test: `backend/tests/unit/test_postconditions.py`

- [ ] Add POI validation (distance/coordinate/metadata/count), buffer validation (area gain/distance tolerance), overlay/clip validation (topology and source fields), raster validation (class counts and nodata), export re-read/feature-count validation, and CRS compatibility validation.
- [ ] Invoke validation after a native tool returns success and before outcome artifact extraction; convert violations to typed error outcomes that block dependent DAG tasks and prevent success text/map emission.
- [ ] Write each validator test first with one valid and one invalid result; run RED then GREEN per validator.

### Task 4: Tighten executable-plan and retry/resume contracts

**Files:**
- Modify: `backend/app/agents/dispatcher.py`, `backend/app/agents/tool_execution.py`, `backend/app/agents/checkpointer.py` as evidence requires
- Test: `backend/tests/unit/test_planner_sources.py`, `backend/tests/unit/test_dispatcher.py`, `backend/tests/integration/test_awaiting_input_e2e.py`

- [ ] Verify root plan role/tool/dependency/argument coverage and hydrate dependency-owned references once server-side.
- [ ] Classify invalid references, provider failures, legal empty results, semantic failures, and cancellation so they cannot retry or end as the wrong terminal status.
- [ ] Make run/resume/checkpoint identity idempotent for repeated resume, refresh, and SSE reconnect before dispatching another spatial operation.

### Task 5: Verify tool facts and live behavior

**Files:**
- Modify: `blackbox/root_planner_synonym_suite.py`, `blackbox/root_planner_synonym_cases.json`
- Output: `blackbox-results/ROOT_PLANNER_SEMANTIC_<timestamp>.json`

- [ ] Add RP01/RP06 golden checks: every displayed POI is within requested radius; answer count equals selected tool count equals map feature count.
- [ ] Run focused unit/integration tests, then live RP01/RP06 against the same public API; capture planner source, payload, SSE sequence, tool terminal statuses, answer, map counts, and failure reasons.
- [ ] Run relevant full backend and Chromium regression suites; stop all test services and record unresolved external-service risks separately from passing evidence.
