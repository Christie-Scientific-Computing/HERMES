# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

HERMES (**H**andles **E**verything: **R**etrieve, **M**odify, **E**xport **S**tuff) is a medical imaging data management app for the Christie NHS Foundation Trust. It centralises radiotherapy planning data from Mosaiq, Pinnacle, and Raystation into a single Orthanc DICOM server, then exports to DICOM modalities or ProKnow.

## Running the App

There are three independently-run components: the **backend** (internal network, one FastAPI app with everything), the **frontend** (the production Django app users actually interact with), and the **proxy** (DMZ, optional — only needed when the frontend itself is external-facing). Most backend/data-model work happens in the backend; most UI/workflow work happens in the frontend.

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

The original Streamlit UI's `pages/` directory (and the entire `gateway/` service, including its own Streamlit `ui/`) were deleted in a 2026-07-30 cleanup. `Home.py` still exists at the repo root but is now orphaned — a multi-page Streamlit entrypoint with no pages under it.

### `frontend/` — the production Django frontend

A Django (ASGI) app: local-account auth (admin-invited only, no public self-registration), an ethics/research-project approval workflow (draft → submitted → approved/rejected/expired/revoked, gating all import/export behind active project membership), and live-progress import/export/results pages. It is the **sole caller** of the backend — every backend call, including SSE batch-job streams, is issued server-side from this project, authenticated by its own session (`request.user`), never by a value the browser supplied. See `frontend/hermes_frontend/backend_client.py` for that boundary and `jobs/views.py`'s `job_stream` for the SSE relay (re-frames the backend's `data: {...}` events with named `event: <type>` lines so the browser's `EventSource` can dispatch per type).

Run it with:

```bash
cd frontend
python manage.py migrate   # once, creates a local db.sqlite3 for Django's own auth/session/admin data
python -m uvicorn hermes_frontend.asgi:application --reload   # ASGI required -- the SSE relay is an async view
```

Reuses the same `BACKEND_URI`/`BACKEND_PORT` convention `webui/` already used (see Environment Variables) — point it directly at `backend/` for an internal-only deployment, or at `proxy/` if this frontend itself is externally/DMZ-reachable. Needs `HERMES_INTERNAL_KEY` to match the backend's own (see below) once that's set.

Apps: `accounts` (users/roles), `research_projects` (ethics workflow; also the one place with a HERMES-specific local model, `ProjectDocument`, for ethics-certificate uploads — everything else project/job-related is backend-owned, fetched fresh via the API), `jobs` (import/export/results, the SSE relay).

**Job → patient drill-down.** The job page's patient list is a shared cotton component (`templates/cotton/patient_table.html`, used by both `job_detail` and `results_lookup`) with `?filter=` pills — `failed`, `not_found`, `missing_mosaiq|pinnacle|proknow` — resolved server-side in the view over the already-fetched summary, no extra backend call. Each MRN links to `jobs/<job_id>/patients/<mrn>/` (`patient_detail`), which shows that patient's Pinnacle plans with a `?status=` filter plus the job-scoped event timeline. Two invariants worth preserving: source presence is **tri-state** (`None` means "never checked", so every `missing_*` predicate tests `is False`, not falsiness), and filter pill counts are computed from the *unfiltered* rows so they don't collapse as you filter. Plans are per-patient, not per-job — the page is job-scoped only so `_job_is_visible_to` governs access.

### `webui/` — throwaway Django test UI (superseded by `frontend/`)

A minimal Django app for manually exercising the backend during development — plain forms for Import/Export/Results, no live SSE progress (it blocks until the batch finishes and shows a results table), no anonymisation-awareness, no auth, no styling beyond readability. Talks directly to `BACKEND_URI`/`BACKEND_PORT`.

**Deprecated as of the ethics-gate work below**: `backend/`'s import/export endpoints now require `project_id`/`username` fields that `webui/` never sends, so its Import/Export pages 422 on every submission. Left as-is rather than patched or removed — it was always meant to be thrown away once a real frontend existed, and that's now `frontend/`. Its Results pages still work (`/results/*` isn't ethics-gated). See the README for `webui/`'s own getting-started steps if you need it for quick backend-only testing regardless.

## Environment Variables

Backend, required in `.env` (not committed) — see `.env.example` for the full annotated list:

| Variable | Purpose |
|---|---|
| `ORTHANC_URL`, `ORTHANC_USER`, `ORTHANC_PASS` | Central DICOM server |
| `BACKEND_URI`, `BACKEND_PORT` | FastAPI location (used by `webui/`, the throwaway test frontend, and reused by `frontend/`, the production one) |
| `DATABASE_URL` | Postgres DSN for HermesDB — job/event tracking today, more HERMES-owned data planned. **Not** the anon-mapping DB below; entirely separate database, never conflate the two |
| `PINN_DB` | Path to Pinnacle's own read-only SQLite export cache (not HERMES-owned) |
| `PINNACLE_SCHEMA` | Postgres schema **inside HermesDB's database** holding PinnacleExport's own `status`/`errors`/`plans` tables. Read-only to HERMES, no Alembic migrations, owned entirely by PinnacleExport. Defaults to `pinnacle_export`; if absent, the patient page reports plans as unavailable rather than erroring |
| `PINNACLE_PUSH_HOST`, `PINNACLE_PUSH_PORT`, `PINNACLE_PUSH_AE_TITLE` | Destination the Pinnacle export submodule pushes to (defaults preserve the historical hardcoded values) |
| `PULL_MODALITY_AET_ONE`, `PULL_MODALITY_AET_TWO` | DICOM AE titles to pull from |
| `PATH_TO_CERT`, `PATH_TO_KEY` | TLS certificates for Orthanc |
| `ANON_DB_HOST`, `ANON_DB_PORT`, `ANON_DB_NAME`, `ANON_DB_USER`, `ANON_DB_PASS` | An **existing, externally-owned, read-only** Postgres database mapping anonymised patient IDs to real ones (`backend/src/identity/anon.py`) — HERMES never writes to it. Unset → passthrough (no anonymisation), the right setting for internal-only deployments. Set these when the backend is reachable from outside the secured network (i.e. fronted by `proxy/`) |
| `HERMES_INTERNAL_KEY` | Optional shared secret, checked via the `X-Hermes-Internal-Key` header on every project-gated route (`/import/*`, `/export/*`, `/projects/*` — see `backend/src/projects/enforcement.py`). Unset → no-op, matching the rest of this table's opt-in style. Set it (matching `frontend/`'s own copy) whenever `frontend/` is reachable from outside the secured network, so "only the frontend calls this backend" is enforced, not just topological |

ProKnow credentials live in `credentials.json` (git-ignored).

Proxy, in `proxy/.env` (see `proxy/.env.example`):

| Variable | Purpose |
|---|---|
| `HERMES_URL` | Backend address the proxy forwards everything to (required) |
| `LOG_LEVEL` | Optional, defaults to `INFO` |

That's the proxy's entire configuration surface — it has no database and no other business logic.

`frontend/`, in its own `.env` (or the repo root `.env` — it loads both, see `frontend/hermes_frontend/settings.py`):

| Variable | Purpose |
|---|---|
| `BACKEND_URI`, `BACKEND_PORT` | Same convention `webui/` already used — where to send every backend call (direct to `backend/`, or to `proxy/` if this frontend is itself external-facing) |
| `HERMES_INTERNAL_KEY` | Must match the backend's own value (above) when set; omit both for a dev/internal-only setup |
| `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS` | Standard Django settings, env-driven; insecure dev defaults if unset — **must** be set for any real deployment |

## Architecture

One FastAPI backend holds every feature (import, export, results, studies). A thin reverse proxy (`proxy/`) can optionally sit in front of it on a separate (DMZ) machine for external access — it carries zero business logic, existing purely to relay HTTP/SSE without exposing the backend's internal network directly. Real patient IDs never cross that boundary: when anonymisation is configured, the backend itself resolves anon ⇄ real IDs at its own API edge (inbound requests, outbound responses/SSE events), so the proxy — and any future external-facing frontend — only ever sees anon IDs.

```
frontend/ (Django, ASGI)       ← the production frontend; SOLE caller of the backend, incl. SSE (see below)
webui/ (Django, test-only)     ← throwaway dev tool, also talks directly to the backend; import/export now 422 (ethics gate)
    │  (HTTP + SSE, + X-Hermes-Internal-Key if HERMES_INTERNAL_KEY is set)
    ▼
proxy/main.py                  ← thin reverse proxy (optional, DMZ machine)
    └── /{path:path}           ← forward.py, forwards everything to HERMES_URL, SSE-aware, no business logic
              │  HTTP + SSE (anon ids only — the backend has already translated by this point)
              ▼
backend/main.py                ← FastAPI app, all features
    ├── /studies*              ← studies/endpoints.py — query Orthanc directly
    ├── /import/*               ← retrieve/endpoints.py — project-gated (see backend/src/projects/enforcement.py)
    ├── /export/*               ← export/endpoints.py — project-gated
    ├── /results/*              ← results/endpoints.py
    └── /projects/*             ← projects/endpoints.py — ethics/research-project workflow, project-gated itself via verify_internal_key
              │
              ├── backend/src/identity/anon.py  ← read-only anon_id ⇄ real_id translation (external DB, see below)
              ├── backend/src/common/sse.py     ← shared SSE batch-job runner (BatchItem, run_batch_job)
              ├── backend/src/projects/         ← ethics/research-project lifecycle + membership + audit log, and the enforcement gate import/export call into
              ├── StatusDB (Postgres, "HermesDB") ← status/db_client.py, via db.py's shared pool
              ├── Orthanc (DICOM hub)  ← via pyorthanc
              ├── ProKnow (cloud RT)   ← via proknow SDK
              └── Pinnacle (local)     ← PinnacleExport submodule
```

**Two entirely separate Postgres databases — never conflate them:**
1. **HermesDB** (`DATABASE_URL`) — fully HERMES-owned, freely migrated (Alembic, `backend/alembic/versions/`). Job/event tracking today; more HERMES-owned data (errors, exports, users) planned.
2. **The anon-mapping DB** (`ANON_DB_*`) — an existing database the Christie team owns, that HERMES only ever runs read-only `SELECT`s against (`backend/src/identity/anon.py`, table `key_value`). Reachable directly from the backend's network. HERMES must never write to it.

**webui pages** (`webui/core/`) — Import (single MRN or CSV batch), Export (DICOM C-MOVE or ProKnow upload), Results (job or patient lookup). See `webui/core/views.py`. (Import/Export are currently broken — see `webui/`'s deprecation note above.)

**frontend apps** (`frontend/`) — `accounts` (local auth, admin-invite), `research_projects` (ethics workflow: create/submit/review/revoke, membership, `ProjectDocument` uploads), `jobs` (single/batch import, DICOM/ProKnow export, results lookup, the SSE relay). See `frontend/hermes_frontend/backend_client.py` for the one place this project talks to the backend, and `frontend/jobs/views.py` for the two-phase batch-job pattern (CSRF-protected POST stages the upload server-side under a fresh job_id in the user's own session; a separate GET-only `job_stream` view is what the browser's `EventSource` connects to).

**Backend modules** (`backend/src/`):
- `retrieve/logic.py` — `Importer` class: searches Mosaiq, Pinnacle, ProKnow; pulls DICOM to Orthanc; runs `_cleanup_orthanc()` to deduplicate and filter by import level. Has characterization tests (`backend/tests/test_cleanup_orthanc.py`) covering every branch, since this is real clinical dedup/pruning logic
- `export/logic.py` — `Exporter` class: C-MOVE to registered modalities or ProKnow SDK upload
- `studies/endpoints.py` — read-only study/series browsing directly against Orthanc's `/tools/find`; translates `patient_id` at the anon boundary and redacts `patient_name` (no mapping exists for names) when anonymisation is configured
- `identity/anon.py` — `resolve_real_id(s)`/`to_display_id(s)`: the anon ⇄ real ID translation boundary. Read-only against the external mapping DB; passthrough when `ANON_DB_HOST` is unset
- `plans/db_client.py` — `PlansDB`: read-only access to PinnacleExport's `plans` table (see HermesDB Schema below). Backs `GET /results/patient/{mrn}/plans` and the frontend's patient-detail page
- `common/sse.py` — `BatchItem`, `run_batch_job()`: the one shared SSE batch-job generator used by every import/export batch endpoint (create job → `start` → per-item cancel-check/StatusDB-write/yield → terminal `{"type": "done"}`). Every event, including the terminal one, carries `"type"`. Also threads `created_by`/`project_id` into `StatusDB.create_job` for traceability
- `status/db_client.py` — `StatusDB`: job/patient/event tracking against HermesDB, via the shared pool in `db.py`. `cancel_job`/`is_cancelled` back cancellation (a column on `jobs`, not an in-process dict — safe under multiple worker processes)
- `projects/` — ethics/research-project workflow, HermesDB-owned (not Django-local): `db_client.py`'s `ProjectsDB` (create/submit/review/revoke, membership, audit log — see HermesDB Schema below), `endpoints.py`'s `/projects` router, and `enforcement.py`'s two fail-closed dependency tiers (`require_any_active_project` for read-only lookups, `require_project_member` for data-moving import/export endpoints) plus `verify_internal_key` (the `HERMES_INTERNAL_KEY` shared-secret check)
- `db.py` — shared `psycopg2` connection pool (`DATABASE_URL`), used by `StatusDB`, `ProjectsDB`, and any future HermesDB-backed module
- `database.py` — runs Alembic migrations (`alembic/versions/`) and initializes the pool on startup

**Proxy modules** (`proxy/`):
- `main.py` — FastAPI app, single catch-all route
- `forward.py` — `proxy_request()`: forwards a request to the backend, transparently handling both JSON and SSE (`text/event-stream`) responses. No anon/PACS logic lives here — see Architecture above for why. Forwards every non-hop-by-hop header verbatim, including `X-Hermes-Internal-Key`, so it's transparent to the ethics-gate enforcement above

## Key Design Patterns

**SSE streaming** — Batch operations stream `text/event-stream` from FastAPI via the shared `run_batch_job()` generator (`backend/src/common/sse.py`). SSE message types: `start`, `progress`, `success`, `error`, `cancelled`, `{"type": "done"}` — every event consistently carries `"type"`. `webui/` (the throwaway test frontend) consumes the whole stream server-side and blocks until it's done rather than showing live progress. `frontend/` (the production one) does the real thing: `jobs/views.py`'s `job_stream` relays the backend's stream live to the browser, re-framing each event with a named `event: <type>` line so a plain `EventSource.addEventListener('progress', ...)` etc. can dispatch per type.

**Cancellation** — Each batch job gets a UUID. `POST /import/cancel/{job_id}` / `POST /export/cancel/{job_id}` call `StatusDB.cancel_job(job_id)`, which sets the `jobs.cancelled` column; `run_batch_job()` checks `StatusDB.is_cancelled(job_id)` once per item. Backed by Postgres rather than an in-process dict, so it works correctly even if the backend runs as multiple worker processes.

**Anonymisation boundary** — When `ANON_DB_*` is configured, every endpoint handling a patient/study identifier resolves inbound anon IDs to real IDs (`backend/src/identity/anon.py`, failing closed with a 422 on unknown IDs) before doing any Mosaiq/Pinnacle/ProKnow/Orthanc work, and translates real IDs back to anon IDs in every outbound response/SSE event. **Structured ID columns aren't the only exposure**: free text carries real MRNs too — `events.error_message` is `str(exception)` from a worker and routinely quotes the id it was handed, `events.details` is a worker's own return value, and Pinnacle's `plans.path`/`comment`/`error_message` are built from or quote the MRN. `results/endpoints.py`'s `_scrub`/`_scrub_json` substitute the anon id into all of those on the way out; anything new that returns worker-generated prose needs the same treatment. The backend is the only place a real ID is ever read or written (logs, HermesDB rows) — it simply never crosses back out to the proxy or any external-facing frontend. Passthrough (no-op) when `ANON_DB_HOST` is unset.

**Async threading** — FastAPI endpoints are `async` but the heavy sync I/O (Orthanc, ProKnow, Pinnacle) runs via `asyncio.to_thread()` (inside `run_batch_job()` for batch jobs) to avoid blocking the event loop.

**Ethics-gate enforcement** — Every import/export endpoint requires `project_id` + `username` (backend/src/retrieve/endpoints.py, backend/src/export/endpoints.py) and calls `backend/src/projects/enforcement.py`'s `require_project_member`/`require_any_active_project` before any CSV parsing, anon-lookup, or StatusDB write. A project must be `approved` and not past its `expiry_date` to count as active; membership and status are re-checked live on every call, never cached. Both dependencies **fail closed**: a DB error checking membership denies (503), it never silently allows — deliberately different from `run_batch_job`'s best-effort "log and continue" tone elsewhere in this codebase, since this is an authorization gate, not bookkeeping. The backend has no authentication of its own; `frontend/` is the only intended caller, session-authenticating the human and attaching `project_id`/`username` itself (never trusting a browser-supplied value) — `HERMES_INTERNAL_KEY` (`verify_internal_key`) is what makes that assumption enforced rather than just topological.

**Import levels** — `Importer._set_import_level()` controls which DICOM modalities are accepted: `Planning data` (CT, RTSTRUCT, RTPLAN, RTDOSE), `Images only` (CT, MR, REG), `Everything` (all).

**Orthanc cleanup** — After importing a patient, `_cleanup_orthanc()` deletes studies lacking RTDOSE (in Planning mode), CBCTs from Elekta manufacturers, and duplicate RTSTRUCT/RTPLAN series that differ between Mosaiq and Pinnacle sources.

## HermesDB Schema (Postgres)

A dedicated, HERMES-owned Postgres database (`DATABASE_URL`), managed via Alembic migrations under `backend/alembic/versions/`. Not the same database as the anon-ID mapping DB (`ANON_DB_*`) — that one is external, read-only, and never touched by these migrations. The `events` table is the core audit log:

```
jobs(job_id, created_at, created_by, description, cancelled, cancelled_at, project_id)
patients(job_id, mrn, input_path, created_at)
events(id, job_id, mrn, stage, event_type, ts, attempt, error_message, details JSONB)

research_projects(project_id, title, description, ethics_reference, status, created_by,
                   reviewed_by, review_comment, submitted_at, approved_at, expiry_date, created_at)
project_memberships(project_id, username, role, added_at)
project_audit_log(id, project_id, username, action, ts, details JSONB)
```

`stage` is `'retrieve'` or `'export'`; `event_type` is `'start'`, `'success'`, or `'failure'`. `jobs.cancelled`/`cancelled_at` back cancellation (see Cancellation above). `jobs.project_id` (nullable, added in `8aa3a51c978c_*`) traces a job back to the ethics-approved project that authorized it — see `common/sse.py`'s `run_batch_job` and `single_import`/`proknow_upload_patient`, which now populate both it and the previously-always-`NULL` `created_by`. `mrn` columns store the real patient ID — only authorised users have access to the backend/HermesDB, so this is fine; the anonymisation boundary is strictly about what crosses back out over HTTP.

**A third set of tables lives in the same database but is NOT HERMES-owned.** PinnacleExport creates and migrates its own schema (`PINNACLE_SCHEMA`, default `pinnacle_export`) inside HermesDB's database:

```
pinnacle_export.plans(id, mrn, path, plan_id, plan_name, plan_date,
                      primary_image_set, pinnacle_version, comment, status, error_message)
pinnacle_export.status(id, mrn, path, process_datetime, status)
pinnacle_export.errors(id, status_id, mrn, path, error_message)
```

HERMES only ever `SELECT`s here (`backend/src/plans/db_client.py`) — **never add an Alembic migration for these tables**; PinnacleExport owns them. `plans.mrn` is the real MRN and joins directly to `events.mrn`, but there's no `job_id`: plans belong to a patient, not a HERMES job, and `(mrn, plan_id)` isn't unique (re-exporting from a different `path` adds a row). `PlansDB.list_plans_for_patient` returns `None` (not `[]`) when the schema is absent, so the UI can distinguish "PinnacleExport isn't deployed here" from "this patient has no plans". Only `plans` is read today; `errors` (joined via `status_id`) is the natural next increment.

`research_projects.status` is one of `draft`/`submitted`/`approved`/`rejected`/`revoked` (see `backend/src/projects/db_client.py`'s `ProjectsDB`); "expired" isn't a separate status, it's `approved` with a past `expiry_date`, computed at query time (`is_project_active`/`is_active_member`). `project_memberships` has no FK to a user table — there isn't one; Django (`frontend/`) is the sole source of truth for user identity, and `username` here is trusted from it. `project_audit_log` is deliberately a separate table from `events` (not a repurposing of it) since `events.mrn` is semantically a patient MRN — project lifecycle is a different concern, mirroring the mutable-state/immutable-audit split pattern from `example_project/plans/models.py` (`ApprovalLog` vs `AuditLog`).

## Testing

`backend/tests/` (pytest) covers `StatusDB` (Phase 1 Postgres migration), `ProjectsDB` and its enforcement dependencies (`test_projects_db.py`, `test_projects_enforcement.py` — lifecycle transitions, expiry/revocation, fail-closed-on-DB-error), the anon boundary (`identity/anon.py` + its wiring into `results`/`studies`/`export`/`import` endpoints), the shared SSE batch-job runner, and characterization tests for `_cleanup_orthanc`. Tests need a real Postgres reachable via `DATABASE_URL` (a throwaway `postgres:16-alpine` container works fine) — there's no mocked/in-memory DB layer. A `conftest.py` fixture, `active_project`, creates a fully-approved project + membership for tests that need to get past the ethics gate. Anon-boundary tests additionally need a second throwaway Postgres (`ANON_DB_*`, seeded with a `key_value` table — see `test_anon.py`'s header for the exact schema) standing in for the externally-owned mapping DB. `test_cleanup_orthanc.py` and `test_retrieve_endpoints_errors.py` additionally need the `PinnacleExport` submodule checked out (they import `retrieve/logic.py`); both skip gracefully via `pytest.importorskip` if the submodule isn't present.

## Git Submodule

`backend/src/retrieve/PinnacleExport/` is a git submodule for Pinnacle DICOM export — `retrieve/logic.py` imports from `backend.src.retrieve.PinnacleExport.entrypoint` and `.src.database`. After cloning, run `git submodule update --init --recursive` to populate it.

(`.gitmodules` previously registered stale paths left over from before an `import_` → `retrieve` rename and didn't match this path — that's been corrected. If you're on a checkout from before this fix, re-run `git submodule sync` before `update --init`.)

## Known Gaps (TODOs in Code)

- Raystation import not implemented
- Metadata editing ("Modify") not implemented anywhere
- ProKnow RTSTRUCT UID regeneration workaround incomplete
- CBCT export and "all images" export option pending
- `Home.py`/`pages/` (Streamlit) and the entire `gateway/` service — including `gateway/ui/` and the PACS-comparison querying (`pacs_client.py`, direct `pynetdicom` C-FIND/C-ECHO) — were deleted in a 2026-07-30 cleanup. PACS-comparison querying would need to be rebuilt from scratch if it's wanted again — nothing references it anymore
- `PinnacleExport`'s own internals haven't been audited beyond the two call sites `retrieve/logic.py` already used (SQL injection fix, env-configurable push destination) — worth a follow-up look at whether it has its own persistence that should eventually join HermesDB
- No root dependency file exists (`requirements.txt` was deleted; a clean one is being rewritten) — see the README for what's currently needed to run each component
- `frontend/` (the production Django frontend, see above) covers Phase 1 of its build-out: auth, the ethics-project workflow's core lifecycle, and live-progress import/export/results. Not yet built (Phase 2): polished ethics-workflow UI details (renewal reminders, richer document handling), a data-availability/cohort catalog browser over `/studies`, an admin compliance/audit-reporting dashboard, and email/in-app notifications (approval decisions, expiry reminders, job completion). The ethics gate itself is intentionally coarse today — project membership gates access to import/export at all, not per-patient/per-cohort scoping
- The coarse ethics gate (`backend/src/projects/enforcement.py`) is enforced by the backend, but the backend has no auth of its own — `HERMES_INTERNAL_KEY` is the only thing standing between "topologically only `frontend/` calls this" and "actually enforced"; it's optional (unset → no-op) and should be treated as required, not optional, for any deployment where the backend is reachable from outside the secured network
