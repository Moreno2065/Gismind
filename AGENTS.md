# AGENTS.md

## Current project contract

Gismind is a single-machine **Agent-driven GIS Runtime**, not a Multi-Sub-Agent
application. The only public execution path is `app.runtime`:

```text
Natural language -> PlannerDraft -> Compiler/PlanIR -> durable Worker
                 -> typed Artifacts + Event Log -> FastAPI/SSE -> React
```

- Runtime facts (Session, Message, Run, Plan, Task, TaskAttempt, Artifact,
  Event and uploads) live in Runtime SQLite plus the local CAS.
- `OpenAICompatiblePlanner` is the only LLM boundary. It emits a constrained
  `PlannerDraft`; the model never invokes handlers, changes retries or writes
  runtime state.
- `ToolContractRegistry.with_builtins()` currently exposes 59 public Runtime
  operations. A handler existing in `app.tools` is not proof that an operation
  has a complete Runtime contract or semantic validation.
- Vector/Raster/Code execution is isolated by the Runtime Worker; FastAPI must
  not directly run GIS handlers.
- SSE is a replayable projection of the SQLite Event Log. Connection state is
  never a Run fact.

The legacy Dispatcher/Sub-Agent/Redis/LangGraph main path and its old tests,
golden/blackbox evidence and old chat/upload endpoints have been removed. Old
`/api/chat` and `/api/upload` routes return `410 LEGACY_RUNTIME_REMOVED`.
`docs/v1/` is read-only historical archive, not a current architecture source.
The precise removal record is
[`docs/runtime_removal_manifest.json`](./docs/runtime_removal_manifest.json).

## Primary design document

Read and update
[`docs/GIS_Runtime_技术架构与实施规格.md`](./docs/GIS_Runtime_技术架构与实施规格.md)
when a Runtime behavior, API, schema, contract or verified deployment fact
changes. It distinguishes target specification from current implementation;
do not describe an unimplemented target as current behavior.

Artifact metadata identity is independent from CAS bytes: v11 removes the
unique `content_hash` constraint, new records use `art_*` IDs, and a shared
blob may back multiple immutable typed Artifacts. Runtime APIs accept
`artifact_id`, never an arbitrary content hash. GC protects the complete
shared-blob group while any sibling is reachable or leased.

Uploads are streamed in 1 MiB chunks into bounded staging. `UPLOAD_MAX_SIZE`
is enforced before CAS publication; ZIP uploads additionally enforce member
count, cumulative uncompressed size, path-traversal and compression-ratio
limits.

## Working rules

1. Preserve typed Artifact references and named ports. Never reintroduce
   `results_data`, numeric output references, session-variable result passing
   or implicit historical output selection.
2. Use the Compiler and ToolContracts to reject missing parameters, invalid
   cardinality and CRS conflicts before execution. Do not hide a contract gap
   by making a generic handler accept arbitrary kwargs.
3. Preserve physical spatial data: Raster Artifacts are readable GeoTIFF
   bytes; CRS metadata must reflect actual coordinates; GCJ02 conversion is
   explicit and never delegated to pyproj as an EPSG code.
4. Make failures durable. A Task business error must lead to a terminal Task
   outcome and correct Run aggregation without stopping the Worker. Planner
   calls renew their planning lease while external model I/O is in progress.
5. Treat `run.awaiting_input` as a paused Run. The frontend must keep enough
   `run_id`, `run_version` and patch-schema context to resume it; do not
   disable the input composer indefinitely.
6. Add a regression test before behavior changes. Prefer runtime-created
   GeoJSON/GeoTIFF/Parquet fixtures; do not restore removed golden evidence or
   deterministic LLM success substitutes.
7. Keep real-model evaluation separate and opt-in (`GISMIND_LIVE_LLM=1`). A
   live action corpus validates LLM -> Draft -> Compiler behavior; it does not
   replace deterministic Runtime tests.

## Common commands

### Backend

```powershell
cd backend
python -m pytest tests -q
python -m pytest tests/unit/test_runtime_vector_contract_semantics.py -q
python -m pytest tests/integration/test_runtime_llm_planner.py -q
python -m app.runtime.gate --phase post-delete
uvicorn app.main:app --reload
```

### Frontend

```powershell
cd frontend
npm run typecheck
npm run build
npm run dev
```

## Important modules

| Area | Current location |
|---|---|
| Public Runtime API and SSE | `backend/app/api/runtime.py` |
| SQLite facts, transitions and events | `backend/app/runtime/store.py`, `db.py` |
| CAS, staging, validation and GC | `backend/app/runtime/artifacts.py` |
| Planner provider boundary | `backend/app/runtime/llm_planner.py` |
| Draft -> PlanIR compiler | `backend/app/runtime/compiler.py` |
| Tool contracts | `backend/app/runtime/contracts.py` |
| Durable leases, Worker and process isolation | `backend/app/runtime/queue.py`, `process.py` |
| Deterministic GIS adapters | `backend/app/runtime/tool_handlers.py` |
| Runtime event reducer | `frontend/src/hooks/useRuntimeRun.ts` |
| Runtime chat/map/artifact UI | `frontend/src/components/ChatPanel.tsx` |

## Verification standard

Before claiming a change complete, run the narrow regression that proves the
reported defect, then the relevant Runtime integration tests. For any code
change touching backend Runtime or frontend behavior, finish with full backend
pytest and frontend typecheck/build unless a concrete external blocker prevents
it. Report warnings separately from assertion failures.
