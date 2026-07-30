# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

HERMES (**H**andles **E**verything: **R**etrieve, **M**odify, **E**xport **S**tuff) is a medical imaging data management app for the Christie NHS Foundation Trust. It centralises radiotherapy planning data from Mosaiq, Pinnacle, and Raystation into a single Orthanc DICOM server, then exports to DICOM modalities or ProKnow.

## Running the App

There are two independently-run components: the **backend** (internal network, one FastAPI app with everything) and the **proxy** (DMZ, optional — only needed for external/user-facing access). Most work happens in the backend.

### Backend

```bash
fastapi run backend/main.py
# or with hot reload:
python -m uvicorn backend.main:app --reload
```

Backend requires `DATABASE_URL` env var (a Postgres DSN) set before it will start — it exits immediately otherwise. On startup it also runs Alembic migrations against that database (`backend/src/database.py` → `alembic upgrade head`), so a fresh/empty Postgres database is all that's needed; no manual schema setup. See the README for full setup steps (Postgres, submodule, `.env`).

### Proxy (optional, DMZ-facing)

One process, run from the `proxy/` directory, with its own `.env` (see `proxy/.env.example`):

```bash
cd proxy
fastapi run main.py --port 8001
```

The proxy requires `HERMES_URL` (pointing at the backend) — it exits immediately otherwise. It carries no business logic or database of its own — it's a pure SSE-aware HTTP forwarder to the backend.

There is currently no working production frontend: the original Streamlit UI's `pages/` directory (and the entire `gateway/` service, including its own Streamlit `ui/`) were deleted in a 2026-07-30 cleanup. `Home.py` still exists at the repo root but is now orphaned — a multi-page Streamlit entrypoint with no pages under it. A production-ready Django frontend is planned but not started (see Known Gaps).

### `webui/` — throwaway Django test UI (not the planned production frontend)

A minimal Django app for manually exercising the backend during development — plain forms for Import/Export/Results, no live SSE progress (it blocks until the batch finishes and shows a results table), no anonymisation-awareness, no auth, no styling beyond readability. Talks directly to `BACKEND_URI`/`BACKEND_PORT`. This is currently the only working frontend in the repo, but it's explicitly **not** the production Django frontend mentioned elsewhere in this doc — that's a separate, not-yet-started effort with real requirements (htmx/SSE live progress, the anon-safe boundary already built into the backend, etc.). Run it with:

```bash
cd webui
python manage.py runserver
```

Needs `python manage.py migrate` once (creates a local `db.sqlite3` for Django's own session/admin machinery — nothing HERMES-specific lives in it). See the README for full getting-started steps.

## Environment Variables

Backend, required in `.env` (not committed) — see `.env.example` for the full annotated list:

| Variable | Purpose |
|---|---|
| `ORTHANC_URL`, `ORTHANC_USER`, `ORTHANC_PASS` | Central DICOM server |
| `BACKEND_URI`, `BACKEND_PORT` | FastAPI location (used by `webui/`, the current test frontend) |
| `DATABASE_URL` | Postgres DSN for HermesDB — job/event tracking today, more HERMES-owned data planned. **Not** the anon-mapping DB below; entirely separate database, never conflate the two |
| `PINN_DB` | Path to Pinnacle's own read-only SQLite export cache (not HERMES-owned) |
| `PINNACLE_PUSH_HOST`, `PINNACLE_PUSH_PORT`, `PINNACLE_PUSH_AE_TITLE` | Destination the Pinnacle export submodule pushes to (defaults preserve the historical hardcoded values) |
| `PULL_MODALITY_AET_ONE`, `PULL_MODALITY_AET_TWO` | DICOM AE titles to pull from |
| `PATH_TO_CERT`, `PATH_TO_KEY` | TLS certificates for Orthanc |
| `ANON_DB_HOST`, `ANON_DB_PORT`, `ANON_DB_NAME`, `ANON_DB_USER`, `ANON_DB_PASS` | An **existing, externally-owned, read-only** Postgres database mapping anonymised patient IDs to real ones (`backend/src/identity/anon.py`) — HERMES never writes to it. Unset → passthrough (no anonymisation), the right setting for internal-only deployments. Set these when the backend is reachable from outside the secured network (i.e. fronted by `proxy/`) |

ProKnow credentials live in `credentials.json` (git-ignored).

Proxy, in `proxy/.env` (see `proxy/.env.example`):

| Variable | Purpose |
|---|---|
| `HERMES_URL` | Backend address the proxy forwards everything to (required) |
| `LOG_LEVEL` | Optional, defaults to `INFO` |

That's the proxy's entire configuration surface — it has no database and no other business logic.

## Architecture

One FastAPI backend holds every feature (import, export, results, studies). A thin reverse proxy (`proxy/`) can optionally sit in front of it on a separate (DMZ) machine for external access — it carries zero business logic, existing purely to relay HTTP/SSE without exposing the backend's internal network directly. Real patient IDs never cross that boundary: when anonymisation is configured, the backend itself resolves anon ⇄ real IDs at its own API edge (inbound requests, outbound responses/SSE events), so the proxy — and any future external-facing frontend — only ever sees anon IDs.

```
webui/ (Django, test-only)     ← current interim frontend (talks directly to the backend)
    │  (HTTP + SSE)
    ▼
proxy/main.py                  ← thin reverse proxy (optional, DMZ machine)
    └── /{path:path}           ← forward.py, forwards everything to HERMES_URL, SSE-aware, no business logic
              │  HTTP + SSE (anon ids only — the backend has already translated by this point)
              ▼
backend/main.py                ← FastAPI app, all features
    ├── /studies*              ← studies/endpoints.py — query Orthanc directly
    ├── /import/*               ← retrieve/endpoints.py
    ├── /export/*               ← export/endpoints.py
    └── /results/*              ← results/endpoints.py
              │
              ├── backend/src/identity/anon.py  ← read-only anon_id ⇄ real_id translation (external DB, see below)
              ├── backend/src/common/sse.py     ← shared SSE batch-job runner (BatchItem, run_batch_job)
              ├── StatusDB (Postgres, "HermesDB") ← status/db_client.py, via db.py's shared pool
              ├── Orthanc (DICOM hub)  ← via pyorthanc
              ├── ProKnow (cloud RT)   ← via proknow SDK
              └── Pinnacle (local)     ← PinnacleExport submodule
```

**Two entirely separate Postgres databases — never conflate them:**
1. **HermesDB** (`DATABASE_URL`) — fully HERMES-owned, freely migrated (Alembic, `backend/alembic/versions/`). Job/event tracking today; more HERMES-owned data (errors, exports, users) planned.
2. **The anon-mapping DB** (`ANON_DB_*`) — an existing database the Christie team owns, that HERMES only ever runs read-only `SELECT`s against (`backend/src/identity/anon.py`, table `key_value`). Reachable directly from the backend's network. HERMES must never write to it.

**webui pages** (`webui/core/`) — Import (single MRN or CSV batch), Export (DICOM C-MOVE or ProKnow upload), Results (job or patient lookup). See `webui/core/views.py`.

**Backend modules** (`backend/src/`):
- `retrieve/logic.py` — `Importer` class: searches Mosaiq, Pinnacle, ProKnow; pulls DICOM to Orthanc; runs `_cleanup_orthanc()` to deduplicate and filter by import level. Has characterization tests (`backend/tests/test_cleanup_orthanc.py`) covering every branch, since this is real clinical dedup/pruning logic
- `export/logic.py` — `Exporter` class: C-MOVE to registered modalities or ProKnow SDK upload
- `studies/endpoints.py` — read-only study/series browsing directly against Orthanc's `/tools/find`; translates `patient_id` at the anon boundary and redacts `patient_name` (no mapping exists for names) when anonymisation is configured
- `identity/anon.py` — `resolve_real_id(s)`/`to_display_id(s)`: the anon ⇄ real ID translation boundary. Read-only against the external mapping DB; passthrough when `ANON_DB_HOST` is unset
- `common/sse.py` — `BatchItem`, `run_batch_job()`: the one shared SSE batch-job generator used by every import/export batch endpoint (create job → `start` → per-item cancel-check/StatusDB-write/yield → terminal `{"type": "done"}`). Every event, including the terminal one, carries `"type"`
- `status/db_client.py` — `StatusDB`: job/patient/event tracking against HermesDB, via the shared pool in `db.py`. `cancel_job`/`is_cancelled` back cancellation (a column on `jobs`, not an in-process dict — safe under multiple worker processes)
- `db.py` — shared `psycopg2` connection pool (`DATABASE_URL`), used by `StatusDB` and any future HermesDB-backed module
- `database.py` — runs Alembic migrations (`alembic/versions/`) and initializes the pool on startup

**Proxy modules** (`proxy/`):
- `main.py` — FastAPI app, single catch-all route
- `forward.py` — `proxy_request()`: forwards a request to the backend, transparently handling both JSON and SSE (`text/event-stream`) responses. No anon/PACS logic lives here — see Architecture above for why

## Key Design Patterns

**SSE streaming** — Batch operations stream `text/event-stream` from FastAPI via the shared `run_batch_job()` generator (`backend/src/common/sse.py`). SSE message types: `start`, `progress`, `success`, `error`, `cancelled`, `{"type": "done"}` — every event consistently carries `"type"`. `webui/` (the current test frontend) consumes the whole stream server-side and blocks until it's done rather than showing live progress; a production frontend would want to actually stream this to the browser (e.g. htmx SSE).

**Cancellation** — Each batch job gets a UUID. `POST /import/cancel/{job_id}` / `POST /export/cancel/{job_id}` call `StatusDB.cancel_job(job_id)`, which sets the `jobs.cancelled` column; `run_batch_job()` checks `StatusDB.is_cancelled(job_id)` once per item. Backed by Postgres rather than an in-process dict, so it works correctly even if the backend runs as multiple worker processes.

**Anonymisation boundary** — When `ANON_DB_*` is configured, every endpoint handling a patient/study identifier resolves inbound anon IDs to real IDs (`backend/src/identity/anon.py`, failing closed with a 422 on unknown IDs) before doing any Mosaiq/Pinnacle/ProKnow/Orthanc work, and translates real IDs back to anon IDs in every outbound response/SSE event. The backend is the only place a real ID is ever read or written (logs, HermesDB rows) — it simply never crosses back out to the proxy or any external-facing frontend. Passthrough (no-op) when `ANON_DB_HOST` is unset.

**Async threading** — FastAPI endpoints are `async` but the heavy sync I/O (Orthanc, ProKnow, Pinnacle) runs via `asyncio.to_thread()` (inside `run_batch_job()` for batch jobs) to avoid blocking the event loop.

**Import levels** — `Importer._set_import_level()` controls which DICOM modalities are accepted: `Planning data` (CT, RTSTRUCT, RTPLAN, RTDOSE), `Images only` (CT, MR, REG), `Everything` (all).

**Orthanc cleanup** — After importing a patient, `_cleanup_orthanc()` deletes studies lacking RTDOSE (in Planning mode), CBCTs from Elekta manufacturers, and duplicate RTSTRUCT/RTPLAN series that differ between Mosaiq and Pinnacle sources.

## HermesDB Schema (Postgres)

A dedicated, HERMES-owned Postgres database (`DATABASE_URL`), managed via Alembic migrations under `backend/alembic/versions/`. Not the same database as the anon-ID mapping DB (`ANON_DB_*`) — that one is external, read-only, and never touched by these migrations. The `events` table is the core audit log:

```
jobs(job_id, created_at, created_by, description, cancelled, cancelled_at)
patients(job_id, mrn, input_path, created_at)
events(id, job_id, mrn, stage, event_type, ts, attempt, error_message, details JSONB)
```

`stage` is `'retrieve'` or `'export'`; `event_type` is `'start'`, `'success'`, or `'failure'`. `jobs.cancelled`/`cancelled_at` back cancellation (see Cancellation above). `mrn` columns store the real patient ID — only authorised users have access to the backend/HermesDB, so this is fine; the anonymisation boundary is strictly about what crosses back out over HTTP.

## Testing

`backend/tests/` (pytest) covers `StatusDB` (Phase 1 Postgres migration), the anon boundary (`identity/anon.py` + its wiring into `results`/`studies`/`export` endpoints), the shared SSE batch-job runner, and characterization tests for `_cleanup_orthanc`. Tests need a real Postgres reachable via `DATABASE_URL` (a throwaway `postgres:16-alpine` container works fine) — there's no mocked/in-memory DB layer. `test_cleanup_orthanc.py` additionally needs the `PinnacleExport` submodule checked out (it imports `retrieve/logic.py`); it skips gracefully via `pytest.importorskip` if the submodule isn't present.

## Git Submodule

`backend/src/retrieve/PinnacleExport/` is a git submodule for Pinnacle DICOM export — `retrieve/logic.py` imports from `backend.src.retrieve.PinnacleExport.entrypoint` and `.src.database`. After cloning, run `git submodule update --init --recursive` to populate it.

(`.gitmodules` previously registered stale paths left over from before an `import_` → `retrieve` rename and didn't match this path — that's been corrected. If you're on a checkout from before this fix, re-run `git submodule sync` before `update --init`.)

## Known Gaps (TODOs in Code)

- Raystation import not implemented
- Metadata editing ("Modify") not implemented anywhere
- ProKnow RTSTRUCT UID regeneration workaround incomplete
- CBCT export and "all images" export option pending
- No production frontend exists. `Home.py`/`pages/` (Streamlit) and the entire `gateway/` service — including `gateway/ui/` and the PACS-comparison querying (`pacs_client.py`, direct `pynetdicom` C-FIND/C-ECHO) — were deleted in a 2026-07-30 cleanup. `webui/` (Django) is a throwaway test UI, not a replacement; a production Django frontend is planned but not started. Since the anon boundary lives entirely in the backend, that future frontend will never need to handle a real ID itself — it can pass anon IDs through everywhere, same as any other identifier. PACS-comparison querying would need to be rebuilt from scratch if it's wanted again — nothing references it anymore
- `PinnacleExport`'s own internals haven't been audited beyond the two call sites `retrieve/logic.py` already used (SQL injection fix, env-configurable push destination) — worth a follow-up look at whether it has its own persistence that should eventually join HermesDB
- No root dependency file exists (`requirements.txt` was deleted; a clean one is being rewritten) — see the README for what's currently needed to run each component
