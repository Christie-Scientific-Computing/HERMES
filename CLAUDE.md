# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

HERMES (**H**andles **E**verything: **R**etrieve, **M**odify, **E**xport **S**tuff) is a medical imaging data management app for the Christie NHS Foundation Trust. It centralises radiotherapy planning data from Mosaiq, Pinnacle, and Raystation into a single Orthanc DICOM server, then exports to DICOM modalities or ProKnow.

## Running the App

There are four independently-run components: the **backend** (internal network, one FastAPI app with everything), the **worker** (polls the backend's own database for queued batch-job tasks and executes them), the **frontend** (`frontend_fastapi/`, the production FastAPI + Jinja2 app users interact with today, as of the Phase 5 cutover), and the **proxy** (DMZ, optional — only needed when the frontend itself is external-facing). Most backend/data-model work happens in the backend; most UI/workflow work happens in the frontend. `frontend/` (Django) is the frontend `frontend_fastapi/` replaced — kept running only for the Phase 6 decommission burn-in period, see below.

`./scripts/dev-up.sh` starts backend + worker(s) + `frontend_fastapi/` together in one terminal (output prefixed per process, torn down together on Ctrl-C) — set `HERMES_DEV_USE_DJANGO_FRONTEND=1` to run the legacy `frontend/` instead. The manual per-component steps below are for running pieces individually, or for the proxy, which that script doesn't cover.

### Backend

```bash
fastapi run backend/main.py
# or with hot reload:
python -m uvicorn backend.main:app --reload
```

Backend requires `DATABASE_URL` env var (a Postgres DSN) set before it will start — it exits immediately otherwise. On startup it also runs Alembic migrations against that database (`backend/src/database.py` → `alembic upgrade head`), so a fresh/empty Postgres database is all that's needed; no manual schema setup. See the README for full setup steps (Postgres, submodule, `.env`).

### Worker

Batch import/export jobs (CSV upload) are no longer executed inline inside the HTTP request — the endpoint enqueues rows onto a `tasks` table and returns immediately; one or more worker processes claim and run them:

```bash
python -m backend.worker
```

Needs the same `DATABASE_URL` as the backend (it uses the same Postgres pool) but deliberately does **not** run Alembic migrations itself — the backend/API process is the sole migrator, so N worker processes never race each other on boot. See `docs/plans/worker-queue-design.md` for the full design and Key Design Patterns below for the runtime behaviour. Scale by running more than one process (`docker compose up --scale worker=3` in the dev compose file); each worker claims one task at a time via Postgres `SELECT ... FOR UPDATE SKIP LOCKED`, so this is safe to do without any extra coordination.

### Proxy (optional, DMZ-facing)

One process, run from the `proxy/` directory, with its own `.env` (see `proxy/.env.example`):

```bash
cd proxy
fastapi run main.py --port 8001
```

The proxy requires `HERMES_URL` (pointing at the backend) — it exits immediately otherwise. It carries no business logic or database of its own — it's a pure SSE-aware HTTP forwarder to the backend.

The original Streamlit UI's `pages/` directory (and the entire `gateway/` service, including its own Streamlit `ui/`) were deleted in a 2026-07-30 cleanup. `Home.py` still exists at the repo root but is now orphaned — a multi-page Streamlit entrypoint with no pages under it.

### `frontend/` — the legacy Django frontend (replaced by `frontend_fastapi/`, see below)

A Django (ASGI) app: local-account auth (admin-invited only, no public self-registration), an ethics/research-project approval workflow (draft → submitted → approved/rejected/expired/revoked, gating all import/export behind active project membership), and live-progress import/export/results pages. **Deprecated as of the Phase 5 cutover** — `frontend_fastapi/` (below) is now the sole caller of the backend; this app is kept running only for the Phase 6 decommission burn-in period (side-by-side comparison, rollback if needed), not for real traffic. What follows describes how it worked while it was the production frontend — every backend call, including SSE batch-job streams, was issued server-side from this project, authenticated by its own session (`request.user`), never by a value the browser supplied. See `frontend/hermes_frontend/backend_client.py` for that boundary and `jobs/views.py`'s `job_stream` for the SSE relay (re-frames the backend's `data: {...}` events with named `event: <type>` lines so the browser's `EventSource` can dispatch per type).

Apps: `accounts` (users/roles), `research_projects` (ethics workflow; also the one place with a HERMES-specific local model, `ProjectDocument`, for ethics-certificate uploads — everything else project/job-related is backend-owned, fetched fresh via the API), `jobs` (import/export/results, the SSE relay). `jobs:submit_job` is the single job-submission page — single patient or batch (CSV), import and/or export (DICOM or ProKnow) all as toggles/checkboxes on one `JobSubmissionForm`, rather than separate pages/tabs per combination; see "Chained export" below for the import→export chaining a submission can opt into via its `do_export` checkbox.

**Job → patient drill-down.** The job page's patient list is a shared cotton component (`templates/cotton/patient_table.html`, used by both `job_detail` and `results_lookup`) with `?filter=` pills — `failed`, `not_found`, `missing_mosaiq|pinnacle|proknow` — resolved server-side in the view over the already-fetched summary, no extra backend call. Each MRN links to `jobs/<job_id>/patients/<mrn>/` (`patient_detail`), which shows that patient's Pinnacle plans with a `?status=` filter plus the job-scoped event timeline. Two invariants worth preserving: source presence is **tri-state** (`None` means "never checked", so every `missing_*` predicate tests `is False`, not falsiness), and filter pill counts are computed from the *unfiltered* rows so they don't collapse as you filter. Plans are per-patient, not per-job — the page is job-scoped only so `_job_is_visible_to` governs access. `job_detail`/`job_watch` grant access via `_job_is_visible_to` (project membership), not a session-staged "this browser started the job" check — a colleague with project access can watch a job they didn't start, and a page refresh doesn't lose the view (see the worker-queue Key Design Pattern below for why that's now possible).

### `frontend_fastapi/` — the FastAPI + Jinja2 rewrite of `frontend/`, now the production frontend

Per `docs/plans/frontend-rewrite-implementation-plan.md`: a phased replacement for `frontend/`, built in parallel rather than in place. **As of Phase 5, this is the sole caller of the backend** — every backend call, including SSE batch-job streams, is issued server-side from this project (`frontend_fastapi/backend_client.py`, the same `X-Hermes-Internal-Key`-attached, session-authenticated boundary `frontend/`'s own client used to enforce), authenticated by its own session, never by a value the browser supplied. `frontend/` (Django) is no longer in the request path for real traffic — see its own section above.

**Status: Phase 0–5 built** (of 6 phases, see the plan doc for full phase breakdown):
- **Phase 0** — scaffolding: hand-rolled DB-backed sessions + CSRF (`session_middleware.py`, `deps.py`), flash messages, auth gates (`exceptions.py`'s `NotAuthenticated`/`Forbidden`), Alembic migrations for this project's own DB, `/health`.
- **Phase 1** — `accounts/` router: login, invite, create-user, activate, user list, plus break-glass CLI scripts (`scripts/reset_password.py`, `scripts/set_staff.py`) for when the UI itself is inaccessible.
- **Phase 2** — `research_projects/` router: the ethics-project workflow (create/submit/review queue/detail).
- **Phase 3a** — `jobs/` router: single/batch import, DICOM/ProKnow export (including combined import→export), results lookup, the SSE-relay equivalent.
- **Phase 4** — `admin/` router (compliance dashboard: project-status counts, expiring-soon list, recent-jobs table, audit-chain status) and `notifications/` (persisted job-done/approval-decision notifications).
- **Phase 5** — cutover: `frontend_fastapi/scripts/migrate_from_django.py` migrates `frontend/`'s local `db.sqlite3` (users, `ProjectDocument` rows) into this project's own DB (dry-run by default, see that script's own docstring for why migrated users get a fresh activation link rather than a carried-over password hash); dev tooling (`scripts/dev-up.sh`, `docker-compose.dev.yml`) now defaults to this app rather than `frontend/`.

**Remaining**: Phase 6 (decommission `frontend/`, once a burn-in period passes with no rollback needed).

**This project has its own local database** (`frontend_fastapi/models.py`, `database.py`), separate from both HermesDB and the anon-mapping DB — `HERMES_FRONTEND_DATABASE_URL` (defaults to a local `db.sqlite3`). It holds only `users`, `sessions` (CSRF token + flash messages live on the session row, so anonymous visitors get a row too), and `project_documents` (ethics-certificate uploads) — i.e. what Django's `auth`/`sessions` contrib apps plus `research_projects.ProjectDocument` gave for free in the `frontend/` version. All job/event/research-project *data* stays backend-owned, fetched fresh via `backend_client.py`; never conflate this DB with HermesDB.

Run it with:

```bash
python -m uvicorn frontend_fastapi.main:app --reload
```

Migrations for this project's own local DB run automatically on startup (`main.py`'s lifespan calls `migrations.run_migrations`), the same pattern the backend uses for HermesDB — no separate migrate step needed.

Reuses the same `BACKEND_URI`/`BACKEND_PORT` convention `webui/` already used (see Environment Variables) — point it directly at `backend/` for an internal-only deployment, or at `proxy/` if this frontend itself is externally/DMZ-reachable. Needs `HERMES_INTERNAL_KEY` to match the backend's own (see below) once that's set. `frontend_fastapi/scripts/dev_seed.py` seeds dev users (mirrors `frontend/`'s `manage.py dev_seed_users`).

### `webui/` — throwaway Django test UI (superseded by `frontend/`)

A minimal Django app for manually exercising the backend during development — plain forms for Import/Export/Results, no live SSE progress (it blocks until the batch finishes and shows a results table), no anonymisation-awareness, no auth, no styling beyond readability. Talks directly to `BACKEND_URI`/`BACKEND_PORT`.

**Deprecated as of the ethics-gate work below**: `backend/`'s import/export endpoints now require `project_id`/`username` fields that `webui/` never sends, so its Import/Export pages 422 on every submission. Left as-is rather than patched or removed — it was always meant to be thrown away once a real frontend existed, and that's now `frontend_fastapi/`. Its Results pages still work (`/results/*` isn't ethics-gated). See the README for `webui/`'s own getting-started steps if you need it for quick backend-only testing regardless.

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
| `ANON_DB_HOST`, `ANON_DB_PORT`, `ANON_DB_NAME`, `ANON_DB_USER`, `ANON_DB_PASS` | An **existing, externally-owned, read-only** Postgres database mapping anonymised patient IDs to real ones (`backend/src/identity/anon.py`) — HERMES never writes to it. Unset (and `ANON_CONFIG` also unset) → passthrough (no anonymisation), the right setting for internal-only deployments. Set these when the backend is reachable from outside the secured network (i.e. fronted by `proxy/`) |
| `ANON_CONFIG`, `ANON_CONFIG_KEY` | Alternative to the five `ANON_DB_*` vars above: `ANON_CONFIG` is a filesystem path to an XML config file (some deployments already have one) holding the same connection details, with the username/password Fernet-encrypted at rest; `ANON_CONFIG_KEY` decrypts them, defaulting to a historical hardcoded key so files already encrypted under it keep working. Takes precedence over `ANON_DB_*` outright when set, not merged field-by-field — see `backend/src/identity/anon.py`'s module docstring |
| `HERMES_INTERNAL_KEY` | Optional shared secret, checked via the `X-Hermes-Internal-Key` header on every project-gated route (`/import/*`, `/export/*`, `/projects/*` — see `backend/src/projects/enforcement.py`). Unset → no-op, matching the rest of this table's opt-in style. Set it (matching `frontend/`'s own copy) whenever `frontend/` is reachable from outside the secured network, so "only the frontend calls this backend" is enforced, not just topological |

ProKnow credentials live in `credentials.json` (git-ignored).

Worker (`backend/worker.py`), same `.env` as the backend plus:

| Variable | Purpose |
|---|---|
| `HERMES_WORKER_POLL_INTERVAL` | Idle poll interval in seconds when the `tasks` queue is empty. Default `2` |
| `HERMES_TASK_STALE_SECONDS` | A claimed-but-not-finished task older than this is assumed to belong to a dead worker and is requeued by the reaper. Default `1800` |

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

`frontend_fastapi/`, same repo-root `.env` plus an optional `frontend_fastapi/.env` (see `frontend_fastapi/settings.py`):

| Variable | Purpose |
|---|---|
| `BACKEND_URI`, `BACKEND_PORT`, `HERMES_INTERNAL_KEY` | Same meaning as `frontend/`'s copies above |
| `HERMES_FRONTEND_DATABASE_URL` | This project's own local DB (users/sessions/`ProjectDocument`) — defaults to a local `db.sqlite3`. **Not** HermesDB and **not** the anon DB |
| `HERMES_FRONTEND_SECRET_KEY` | Session/CSRF signing key; insecure dev default if unset — must be set for any real deployment |
| `HERMES_FRONTEND_DEBUG`, `HERMES_FRONTEND_ALLOWED_HOSTS` | Analogous to Django's `DEBUG`/`ALLOWED_HOSTS` |
| `HERMES_FRONTEND_MEDIA_ROOT` | Where `ProjectDocument` uploads are stored on disk |
| `HERMES_FRONTEND_SMTP_HOST` etc. | Optional outbound email for invite/activation links; unset → logs the message instead of sending (no SMTP relay assumed) |

## Architecture

One FastAPI backend holds every feature (import, export, results, studies). A thin reverse proxy (`proxy/`) can optionally sit in front of it on a separate (DMZ) machine for external access — it carries zero business logic, existing purely to relay HTTP/SSE without exposing the backend's internal network directly. Real patient IDs never cross that boundary: when anonymisation is configured, the backend itself resolves anon ⇄ real IDs at its own API edge (inbound requests, outbound responses/SSE events), so the proxy — and any future external-facing frontend — only ever sees anon IDs.

```
frontend_fastapi/ (FastAPI, Jinja2) ← the production frontend, as of the Phase 5 cutover; SOLE caller of the backend, incl. SSE (see below)
frontend/ (Django, ASGI)       ← the legacy frontend it replaced -- kept running only for the Phase 6 decommission burn-in period, not real traffic
webui/ (Django, test-only)     ← throwaway dev tool, also talks directly to the backend; import/export now 422 (ethics gate)
    │  (HTTP + SSE, + X-Hermes-Internal-Key if HERMES_INTERNAL_KEY is set)
    ▼
proxy/main.py                  ← thin reverse proxy (optional, DMZ machine)
    └── /{path:path}           ← forward.py, forwards everything to HERMES_URL, SSE-aware, no business logic
              │  HTTP + SSE (anon ids only — the backend has already translated by this point)
              ▼
backend/main.py                ← FastAPI app, all features
    ├── /studies*              ← studies/endpoints.py — query Orthanc directly
    ├── /import/*               ← retrieve/endpoints.py — project-gated; CSV batches enqueue onto tasks (see below), single-patient stays synchronous
    ├── /export/*               ← export/endpoints.py — project-gated; same enqueue-vs-synchronous split as import
    ├── /results/*              ← results/endpoints.py — job/patient lookups, + GET /results/job/{job_id}/stream (polls tasks, emits the observer SSE stream)
    └── /projects/*             ← projects/endpoints.py — ethics/research-project workflow, project-gated itself via verify_internal_key
              │
              ├── backend/src/identity/anon.py  ← read-only anon_id ⇄ real_id translation (external DB, see below)
              ├── backend/src/status/tasks_db.py ← TasksDB: enqueue/claim/mark_* against the `tasks` queue (Postgres SKIP LOCKED)
              ├── backend/src/common/sse.py     ← shared SSE batch-job runner (BatchItem, run_batch_job) — still used for the non-file "list of MRNs" batch alias; CSV-upload batches go through the queue instead
              ├── backend/src/projects/         ← ethics/research-project lifecycle + membership + audit log, and the enforcement gate import/export call into
              ├── StatusDB (Postgres, "HermesDB") ← status/db_client.py, via db.py's shared pool
              ├── Orthanc (DICOM hub)  ← via pyorthanc
              ├── ProKnow (cloud RT)   ← via proknow SDK
              └── Pinnacle (local)     ← PinnacleExport submodule
                        ▲
                        │  claims tasks, calls the SAME worker factories the synchronous endpoints use
backend/worker.py (one or more processes) ← python -m backend.worker; polls `tasks`, no business logic of its own
```

**Batch job lifecycle (CSV import/export):** POST endpoint parses the CSV, resolves anon IDs, writes `jobs`/`patients` rows, and inserts one `tasks` row per patient (state `queued`) — then returns `{job_id, total}` immediately; the HTTP request never runs the actual work. `backend/worker.py` claims rows one at a time (`SELECT ... FOR UPDATE SKIP LOCKED`), re-checks the ethics gate (a project can be revoked between enqueue and execution), then calls the exact same `_import_worker`/`_dicom_move_worker`/`_proknow_worker`/`_uid_move_worker` factories the old synchronous path used — no duplicated business logic. `frontend/`'s `job_stream` relays `GET /results/job/{job_id}/stream` (an endpoint that polls the `tasks` table roughly once a second and emits the same `start`/`progress`/`success`/`error`/`cancelled`/`done` vocabulary the old in-request SSE stream produced), so closing the browser tab no longer kills the job, a page refresh doesn't lose it, and a colleague with project access can watch it too. See `docs/plans/worker-queue-design.md` for the full design and rationale.

**Two entirely separate Postgres databases — never conflate them:**
1. **HermesDB** (`DATABASE_URL`) — fully HERMES-owned, freely migrated (Alembic, `backend/alembic/versions/`). Job/event tracking today; more HERMES-owned data (errors, exports, users) planned.
2. **The anon-mapping DB** (`ANON_DB_*`) — an existing database the Christie team owns, that HERMES only ever runs read-only `SELECT`s against (`backend/src/identity/anon.py`, table `key_value`). Reachable directly from the backend's network. HERMES must never write to it.

**webui pages** (`webui/core/`) — Import (single MRN or CSV batch), Export (DICOM C-MOVE or ProKnow upload), Results (job or patient lookup). See `webui/core/views.py`. (Import/Export are currently broken — see `webui/`'s deprecation note above.)

**frontend apps** (`frontend/`) — `accounts` (local auth, admin-invite), `research_projects` (ethics workflow: create/submit/review/revoke, membership, `ProjectDocument` uploads), `jobs` (single/batch import, DICOM/ProKnow export, results lookup, the SSE relay). See `frontend/hermes_frontend/backend_client.py` for the one place this project talks to the backend, and `frontend/jobs/views.py` for the CSRF-protected POST that calls the backend's enqueue endpoint and redirects, plus the separate GET-only `job_stream` view the browser's `EventSource` connects to (relaying the backend's observer stream — see the worker queue lifecycle above; no session-staged upload dance any more).

**Backend modules** (`backend/src/`):
- `retrieve/logic.py` — `Importer` class: searches Mosaiq, Pinnacle, ProKnow; pulls DICOM to Orthanc; runs `_cleanup_orthanc()` to deduplicate and filter by import level. Has characterization tests (`backend/tests/test_cleanup_orthanc.py`) covering every branch, since this is real clinical dedup/pruning logic
- `export/logic.py` — `Exporter` class: C-MOVE to registered modalities or ProKnow SDK upload
- `studies/endpoints.py` — read-only study/series browsing directly against Orthanc's `/tools/find`; translates `patient_id` at the anon boundary and redacts `patient_name` (no mapping exists for names) when anonymisation is configured
- `identity/anon.py` — `resolve_real_id(s)`/`to_display_id(s)`: the anon ⇄ real ID translation boundary. Read-only against the external mapping DB; passthrough when neither `ANON_DB_HOST` nor `ANON_CONFIG` is set. Credentials come from either plain `ANON_DB_*` env vars or an `ANON_CONFIG` XML file with Fernet-encrypted username/password (`ANON_CONFIG` wins when both are set)
- `plans/db_client.py` — `PlansDB`: read-only access to PinnacleExport's `plans` table (see HermesDB Schema below). Backs `GET /results/patient/{mrn}/plans` and the frontend's patient-detail page
- `common/sse.py` — `BatchItem`, `run_batch_job()`: the SSE generator (create job → `start` → per-item cancel-check/StatusDB-write/yield → terminal `{"type": "done"}`). Still used for the non-file "list of MRNs" batch alias endpoints; CSV-upload batches (the common case) now go through `tasks_db.py` + `backend/worker.py` instead — see the worker queue lifecycle above. `BatchItem` and the per-flow worker factories (`_import_worker`, `_dicom_move_worker`, etc.) are shared by both paths
- `status/tasks_db.py` — `TasksDB`: the `tasks` queue backing CSV-upload batch jobs — `enqueue()` (called by the API process at submit time), `claim()` (Postgres `SELECT ... FOR UPDATE SKIP LOCKED`, called by `backend/worker.py`), `mark_running`/`mark_succeeded`/`mark_failed` (ownership-guarded by `claimed_by`), `cancel_task`, `reap_stale_claims`. See `docs/plans/worker-queue-design.md`
- `status/db_client.py` — `StatusDB`: job/patient/event tracking against HermesDB, via the shared pool in `db.py`. `cancel_job`/`is_cancelled` back cancellation (a column on `jobs`, not an in-process dict — safe under multiple worker processes)
- `status/hash_chain.py` — canonical JSON + hashing shared by `StatusDB.add_event` (writes the chain) and `backend/scripts/verify_audit_chain.py` (verifies it after the fact), so `events` rows are tamper-evident (docs/plans/safety-plan.md §D1)
- `projects/` — ethics/research-project workflow, HermesDB-owned (not Django-local): `db_client.py`'s `ProjectsDB` (create/submit/review/revoke, membership, audit log — see HermesDB Schema below), `endpoints.py`'s `/projects` router, and `enforcement.py`'s two fail-closed dependency tiers (`require_any_active_project` for read-only lookups, `require_project_member` for data-moving import/export endpoints) plus `verify_internal_key` (the `HERMES_INTERNAL_KEY` shared-secret check)
- `db.py` — shared `psycopg2` connection pool (`DATABASE_URL`), used by `StatusDB`, `ProjectsDB`, and any future HermesDB-backed module
- `database.py` — runs Alembic migrations (`alembic/versions/`) and initializes the pool on startup

**Proxy modules** (`proxy/`):
- `main.py` — FastAPI app, single catch-all route
- `forward.py` — `proxy_request()`: forwards a request to the backend, transparently handling both JSON and SSE (`text/event-stream`) responses. No anon/PACS logic lives here — see Architecture above for why. Forwards every non-hop-by-hop header verbatim, including `X-Hermes-Internal-Key`, so it's transparent to the ethics-gate enforcement above

## Key Design Patterns

**SSE streaming** — Every SSE stream, whichever backs it, emits the same vocabulary: `start`, `progress`, `success`, `error`, `cancelled`, `total`, `{"type": "done"}` — every event consistently carries `"type"`. Two producers today: the shared `run_batch_job()` generator (`backend/src/common/sse.py`, used for the non-file "list of MRNs" batch alias and by `webui/`, which consumes the whole stream server-side and blocks rather than showing live progress) and the observer stream (`GET /results/job/{job_id}/stream`, `results/endpoints.py`, used for CSV-upload batches — polls the `tasks` table roughly once a second). The observer's `progress`/`success`/`error` events additionally carry `"stage"` (`retrieve`/`export`), and its `start`/`total` events carry `"import_total"`/`"export_total"` alongside the overall `"total"` — both computed fresh from the current task rows on every tick, not inferred by a consumer, so a reconnect (refresh, a colleague joining, `EventSource`'s own auto-reconnect) reports the same split a continuous connection would have. `frontend/` (the production one) does the real thing either way: `jobs/views.py`'s `job_stream` relays the backend's stream live to the browser, re-framing each event with a named `event: <type>` line so a plain `EventSource.addEventListener('progress', ...)` etc. can dispatch per type.

**Chained export (combined import→export jobs)** — `backend/src/retrieve/endpoints.py`'s `batch_import_file` accepts optional `export_kind`/`destination`/`collection`/`message_id` fields; when given, each enqueued import task's `params` carries a `chain_export` block. `backend/worker.py`'s `_maybe_chain_export` runs after a successful import — if the patient was actually found (`details["imported"]`, not just "the import ran without raising") and the job isn't cancelled — and enqueues a matching `dicom_move`/`proknow_upload` task on the **same `job_id`**. The enqueue happens **before** `mark_succeeded` for the import task, not after: the observer decides a job is `done` once nothing is pending, so enqueueing after would open a window where a poll could see zero pending tasks and end the stream while the export was about to start unwatched. A second chain attempt for the same `(job_id, kind, status_mrn)` — most commonly a task reaped from a slow worker and reclaimed by another, both completing it, but equally a combined CSV that happens to list the same patient twice — is a no-op at the database level: `tasks.chained_from_task_id` plus a partial unique index on `(job_id, kind, status_mrn) WHERE chained_from_task_id IS NOT NULL`, checked via `TasksDB.enqueue`'s `ON CONFLICT DO NOTHING` — not an application-level read-then-write check, which two genuinely concurrent workers could both pass. The dedup key doesn't distinguish *which* import task chained it (only `job_id`/`kind`/`status_mrn`), so it's stricter than "the reap case" alone — deliberately, since silently dropping a second real DICOM C-MOVE/ProKnow upload for the same patient is the safe failure mode here, not an unwanted one. `frontend/`'s `jobs:submit_job` is the one submission form for this (its `do_export` checkbox, alongside `do_import`); `job_watch` picks the two-stage `<c-combined-job-progress>` component over the single-stage `<c-job-progress>` via `job_summary`'s `is_combined` field (`TasksDB.job_has_chain_export`, true from submission, not just once a task has actually chained).

**Cancellation** — Each batch job gets a UUID. `POST /import/cancel/{job_id}` / `POST /export/cancel/{job_id}` call `StatusDB.cancel_job(job_id)`, which sets the `jobs.cancelled` column. `run_batch_job()` checks `StatusDB.is_cancelled(job_id)` once per item; for queue-driven jobs, cancelling additionally does `UPDATE tasks SET state='cancelled' WHERE job_id=... AND state='queued'` (`TasksDB.cancel_queued`), strictly stronger than the per-item check since already-`running` tasks still finish but nothing new is claimed. Backed by Postgres rather than an in-process dict, so it works correctly with multiple worker processes.

**Audit trail** — `events` rows form a hash chain (`status/hash_chain.py`, `StatusDB.add_event`): each row's hash covers its own canonical-JSON content plus the previous row's hash, so an edited or deleted row breaks verification. `backend/scripts/verify_audit_chain.py` re-walks the chain and reports the first break. Export jobs additionally record a manifest on their `Response` (series/instance counts, study/series UIDs, a per-instance checksum — algorithm varies by destination, see `export/endpoints.py`'s `Response` model docstring) rather than the old bare `{"status": "Success"}`. See `docs/plans/safety-plan.md` §D for the design and what's still open.

**Anonymisation boundary** — When `ANON_DB_*` is configured, every endpoint handling a patient/study identifier resolves inbound anon IDs to real IDs (`backend/src/identity/anon.py`, failing closed with a 422 on unknown IDs) before doing any Mosaiq/Pinnacle/ProKnow/Orthanc work, and translates real IDs back to anon IDs in every outbound response/SSE event. **Structured ID columns aren't the only exposure**: free text carries real MRNs too — `events.error_message` is `str(exception)` from a worker and routinely quotes the id it was handed, `events.details` is a worker's own return value, and Pinnacle's `plans.path`/`comment`/`error_message` are built from or quote the MRN. `results/endpoints.py`'s `_scrub`/`_scrub_json` substitute the anon id into all of those on the way out; anything new that returns worker-generated prose needs the same treatment. The backend is the only place a real ID is ever read or written (logs, HermesDB rows) — it simply never crosses back out to the proxy or any external-facing frontend. Passthrough (no-op) when neither `ANON_DB_HOST` nor `ANON_CONFIG` is set.

This boundary isn't just a real↔anon id swap — see `docs/pii-boundary-safety.md` for the full risk register, but the pieces worth knowing when touching any outbound endpoint:
- `backend/src/common/pii_patterns.py` — `redact()`/`redact_dict()`, the general-pattern floor: catches dates, DICOM UIDs, filesystem paths, and DB connection-string shapes generically (not just one known MRN), plus zero-padded/float-cast format variants of a known real id. `redact_dict`'s `NON_PII_STRUCTURAL_FIELDS` (`mrn`, `destination`, `destination_type`, `submitted_by`) are passed through untouched by design — operational config, not patient data, that the generic floor would otherwise mangle if it happens to look date/UID-shaped (e.g. a ProKnow collection name like `"Trial_2024-01-15_Cohort"`).
- `backend/src/identity/anon.py`'s `shift_date(real_id, date_str)` — clinical dates (`study_date`/`series_date`/`plan_date`) are **shifted** by a per-patient day offset (`key_value.date_perturbation`), not redacted, preserving relative clinical intervals while breaking the link to the real calendar date.
- `backend/src/common/sse.py`'s `to_public_details(details)` — strips real DICOM UIDs (`study_uids`/`series_uids` dropped entirely, `checksums` re-keyed from `dict[SOPInstanceUID, hash]` to a plain `list[str]`) from every outbound success event/response, while `events.details`/`tasks.details` keep full fidelity in the DB for audit. There is no legitimate "shifted UID" the way there is a shifted date — DICOM UIDs are forbidden crossing the boundary at all, no exceptions.
- `backend/src/common/errors.py`'s `register_pii_safe_exception_handlers` — a single global handler (registered once in `backend/main.py`) runs every `HTTPException.detail`, anywhere in the app, through `pii_patterns.redact()`'s generic floor before the response is sent. Deliberately generic-pattern-only (no real-id substitution — the handler has no request-scoped real id to substitute) — a safety net for the *unexpected* uncaught case, not a replacement for a call site's own precise real-id/display-id substitution.
- **Known, accepted residual gap**: none of the above catches identifying information embedded in arbitrary free text that doesn't match a structural pattern — a clinician typing a patient's name into a DICOM series description, or a date written as prose in a comment field. Pattern-based detection catches shapes, not arbitrary prose; there's no proposed fix.

**Async threading** — FastAPI endpoints are `async` but the heavy sync I/O (Orthanc, ProKnow, Pinnacle) runs via `asyncio.to_thread()` (inside `run_batch_job()` for the SSE-generator path). `backend/worker.py` is a plain synchronous process, not asyncio at all — one task in flight per process, so this concern doesn't apply there; scale with more processes instead (see Worker above).

**Ethics-gate enforcement** — Every import/export endpoint requires `project_id` + `username` (backend/src/retrieve/endpoints.py, backend/src/export/endpoints.py) and calls `backend/src/projects/enforcement.py`'s `require_project_member`/`require_any_active_project` before any CSV parsing, anon-lookup, or StatusDB write. A project must be `approved` and not past its `expiry_date` to count as active; membership and status are re-checked live on every call, never cached. Both dependencies **fail closed**: a DB error checking membership denies (503), it never silently allows — deliberately different from `run_batch_job`'s best-effort "log and continue" tone elsewhere in this codebase, since this is an authorization gate, not bookkeeping. The backend has no authentication of its own; `frontend_fastapi/` is the only intended caller (as of the Phase 5 cutover; `frontend/` before it), session-authenticating the human and attaching `project_id`/`username` itself (never trusting a browser-supplied value) — `HERMES_INTERNAL_KEY` (`verify_internal_key`) is what makes that assumption enforced rather than just topological.

Project approval is a single yes/no gate on tool access, not a bound on which patients or how much data a member may subsequently export. Approving a project is equivalent to trusting every member with unrestricted pseudo-anonymised export for the life of the approval.

**Import levels** — `Importer._set_import_level()` controls which DICOM modalities are accepted: `Planning data` (CT, RTSTRUCT, RTPLAN, RTDOSE), `Images only` (CT, MR, REG), `Everything` (all).

**Orthanc cleanup** — After importing a patient, `_cleanup_orthanc()` deletes studies lacking RTDOSE (in Planning mode), CBCTs from Elekta manufacturers, and duplicate RTSTRUCT/RTPLAN series that differ between Mosaiq and Pinnacle sources.

## HermesDB Schema (Postgres)

A dedicated, HERMES-owned Postgres database (`DATABASE_URL`), managed via Alembic migrations under `backend/alembic/versions/`. Not the same database as the anon-ID mapping DB (`ANON_DB_*`) — that one is external, read-only, and never touched by these migrations. The `events` table is the core audit log:

```
jobs(job_id, created_at, created_by, description, cancelled, cancelled_at, project_id)
patients(job_id, mrn, input_path, created_at)
events(id, job_id, mrn, stage, event_type, ts, attempt, error_message, details JSONB,
       task_id, prev_hash, row_hash)

tasks(task_id, job_id, kind, stage, state, real_id, display_id, status_mrn, input_path,
      extra JSONB, params JSONB, priority, attempts, max_attempts, claimed_by, claimed_at,
      created_at, started_at, finished_at, error_message, details JSONB, chained_from_task_id)

event_chain_state(id, last_hash)   -- singleton row; the hash chain's running tip, see Audit trail above

research_projects(project_id, title, description, ethics_reference, status, created_by,
                   reviewed_by, review_comment, submitted_at, approved_at, expiry_date, created_at)
project_memberships(project_id, username, role, added_at)
project_audit_log(id, project_id, username, action, ts, details JSONB)
```

`stage` is `'retrieve'` or `'export'`; `event_type` is `'start'`, `'success'`, or `'failure'`. `jobs.cancelled`/`cancelled_at` back cancellation (see Cancellation above). `jobs.project_id` traces a job back to the ethics-approved project that authorized it — populated by `common/sse.py`'s `run_batch_job` and by `tasks_db.enqueue()`. `mrn` columns store the real patient ID — only authorised users have access to the backend/HermesDB, so this is fine; the anonymisation boundary is strictly about what crosses back out over HTTP. `events.prev_hash`/`row_hash` are the hash chain (see Audit trail above); `events.task_id` links an event back to the queue-driven task that produced it (`NULL` for the still-synchronous SSE-generator path).

`tasks` is the queue CSV-upload batch jobs run on (see the worker queue lifecycle above and `docs/plans/worker-queue-design.md`). `state` is one of `queued`/`claimed`/`running`/`succeeded`/`failed`/`cancelled`; `kind` is `import`/`dicom_move`/`proknow_upload`/`uid_move`. `real_id` is always the real identifier passed to Mosaiq/Pinnacle/ProKnow/Orthanc — for the UID-export flow this is a DICOM study UID, not an MRN, which is why the row also carries a separate `status_mrn` (what `events`/StatusDB key on) and `extra` (the study/series UIDs the worker factory actually needs). `tasks` is mutable current state; `events` stays the immutable audit log — deliberately not merged, mirroring the same split already chosen for `research_projects` vs `project_audit_log`. `chained_from_task_id` (nullable, FK to `tasks.task_id`) is set only on a task `backend/worker.py`'s `_maybe_chain_export` enqueued — NULL for every ordinary batch submission — and backs a partial unique index on `(job_id, kind, status_mrn)` that makes a duplicate chained enqueue for the same patient/job/export-kind a no-op rather than a second real export (see "Chained export" above). Scoped to `chained_from_task_id IS NOT NULL` specifically so it never applies to a plain (non-chained) batch export submission — a duplicate patient row in an ordinary export CSV still enqueues two tasks today, unchanged (`test_tasks_db.py`).

**A third set of tables lives in the same database but is NOT HERMES-owned.** PinnacleExport creates and migrates its own schema (`PINNACLE_SCHEMA`, default `pinnacle_export`) inside HermesDB's database:

```
pinnacle_export.plans(id, mrn, path, plan_id, plan_name, plan_date,
                      primary_image_set, pinnacle_version, comment, status, error_message)
pinnacle_export.status(id, mrn, path, process_datetime, status)
pinnacle_export.errors(id, status_id, mrn, path, error_message)
```

HERMES only ever `SELECT`s here (`backend/src/plans/db_client.py`) — **never add an Alembic migration for these tables**; PinnacleExport owns them. `plans.mrn` is the real MRN and joins directly to `events.mrn`, but there's no `job_id`: plans belong to a patient, not a HERMES job, and `(mrn, plan_id)` isn't unique (re-exporting from a different `path` adds a row). `PlansDB.list_plans_for_patient` returns `None` (not `[]`) when the schema is absent, so the UI can distinguish "PinnacleExport isn't deployed here" from "this patient has no plans". Only `plans` is read today; `errors` (joined via `status_id`) is the natural next increment.

`research_projects.status` is one of `draft`/`submitted`/`approved`/`rejected`/`revoked` (see `backend/src/projects/db_client.py`'s `ProjectsDB`); "expired" isn't a separate status, it's `approved` with a past `expiry_date`, computed at query time (`is_project_active`/`is_active_member`). `project_memberships` has no FK to a user table — there isn't one; the frontend (`frontend_fastapi/` as of Phase 5, `frontend/` before it) is the sole source of truth for user identity, and `username` here is trusted from it. `project_audit_log` is deliberately a separate table from `events` (not a repurposing of it) since `events.mrn` is semantically a patient MRN — project lifecycle is a different concern, mirroring the mutable-state/immutable-audit split pattern from `example_project/plans/models.py` (`ApprovalLog` vs `AuditLog`).

## Testing

`backend/tests/` (pytest) covers `StatusDB`, `ProjectsDB` and its enforcement dependencies (`test_projects_db.py`, `test_projects_enforcement.py` — lifecycle transitions, expiry/revocation, fail-closed-on-DB-error), the anon boundary (`identity/anon.py` + its wiring into `results`/`studies`/`export`/`import` endpoints), the hash chain (`test_hash_chain.py`) and export manifest (`test_export_manifest.py`, `test_export_manifest_shape.py`), the `tasks` queue (`test_tasks_db.py` — enqueue counts, concurrent-claim exclusivity, `reap_stale_claims`), the worker's claim/execute loop (`test_worker.py`), the observer stream (`test_observer_stream.py`), the legacy SSE generator (`test_sse.py`, kept passing alongside the queue path), and characterization tests for `_cleanup_orthanc`. Tests need a real Postgres reachable via `DATABASE_URL` (a throwaway `postgres:16-alpine` container works fine) — there's no mocked/in-memory DB layer. A `conftest.py` fixture, `active_project`, creates a fully-approved project + membership for tests that need to get past the ethics gate. Anon-boundary tests additionally need a second throwaway Postgres (`ANON_DB_*`, seeded with a `key_value` table — `backend/scripts/seed_anon_test_db.py` seeds the exact fixed real↔anon pairs these tests hardcode, reproducibly) standing in for the externally-owned mapping DB. `test_cleanup_orthanc.py` and `test_retrieve_endpoints_errors.py` additionally need the `PinnacleExport` submodule checked out (they import `retrieve/logic.py`); both skip gracefully via `pytest.importorskip` if the submodule isn't present.

**PII-boundary-specific test files** (`backend/tests/support/pii_assertions.py`'s `assert_no_pii`/`assert_date_shifted_correctly`, built directly on `pii_patterns.py` so the suite always checks against exactly what production code redacts): `test_pii_patterns.py`/`test_pii_assertions.py` (the shared infrastructure itself), `test_anon_date_shift.py` (`shift_date`), `test_export_manifest_shape.py` (UID-stripping shape), `test_batch_alias_pii_boundary.py` (`run_batch_job`'s three JSON-bodied SSE consumers, success and induced worker failure), `test_single_item_pii_boundary.py` (`single_import`/`find_patient`/`proknow_upload_patient`), `test_http_exception_pii_boundary.py` (the global exception handler), plus the rewritten `test_results_anon_boundary.py`/`test_export_anon_boundary.py`/`test_studies_anon_boundary.py`.

**CI**: `.github/workflows/test.yml` runs the full suite above on every `pull_request`/`push` to `main`, against two ephemeral Postgres 16 service containers (HermesDB + the anon-mapping-DB stand-in), migrating HermesDB via `alembic upgrade head` and seeding the anon-test DB via the script above before `pytest`. `PinnacleExport` is fetched via a separate, explicitly non-fatal step (reusing the same `PINNACLE_EXPORT_PAT` secret `Dockerfile`/`docker-publish.yml` already use for it) so a missing/inaccessible submodule only shrinks what pytest can run, never fails the job outright.

`frontend_fastapi/tests/` (pytest) covers what's built so far: sessions/CSRF (`test_sessions.py`, `test_security.py`), auth deps (`test_deps.py`), the `accounts` router (login/invite/activate/create-user/user-list), `research_projects`, the backend client, migrations, and the break-glass scripts. Uses its own local DB (SQLite in-memory or a throwaway Postgres via `HERMES_FRONTEND_DATABASE_URL`), separate from the backend's Postgres fixtures above.

**CI** (`.github/workflows/test.yml`) only runs `backend/tests/` (not `frontend_fastapi/tests/`), against two ephemeral `postgres:16-alpine` service containers (HermesDB on host port 55432, the anon-DB stand-in on 55433 — ports match `conftest.py`/`test_*_anon_boundary.py`'s hardcoded defaults). It runs on **Python 3.13**, not the 3.12 used elsewhere historically: `PinnacleExport`'s own source uses the single-type-argument `Generator[tuple[str, str]]` shorthand, which needs stdlib default type params that only landed in 3.13 — on 3.12 the same import raises `TypeError: Too few arguments for typing.Generator`. This was previously misdiagnosed a few times as a dependency-tree conflict (see the workflow file's own step comments for the full history) before being root-caused to the Python version alone. The `PinnacleExport` checkout itself is best-effort (a PAT-authenticated `git clone`, not `actions/checkout`'s submodules option) so a missing/invalid `PINNACLE_EXPORT_PAT` secret degrades to `pytest.importorskip`-skipped tests rather than failing the job; when it does check out, its `requirements.txt` is installed *before* `requirements-dev.txt` so this repo's own `pytest`/`httpx` pins win. `pytest` runs with `--continue-on-collection-errors` so one file's import failure can't zero out the whole run's signal.

## Git Submodule

`backend/src/retrieve/PinnacleExport/` is a git submodule for Pinnacle DICOM export — `retrieve/logic.py` imports from `backend.src.retrieve.PinnacleExport.entrypoint` and `.src.database`. After cloning, run `git submodule update --init --recursive` to populate it.

(`.gitmodules` previously registered stale paths left over from before an `import_` → `retrieve` rename and didn't match this path — that's been corrected. If you're on a checkout from before this fix, re-run `git submodule sync` before `update --init`.)

## Known Gaps / TODO

See also `docs/known-issues.md` (export-governance review findings, most now addressed — kept as the historical record) and `docs/plans/safety-plan.md` (the design doc for that work, §-numbered, useful for "why does this field/table exist" questions). The DMZ-proxy PII boundary (MRNs, dates, DICOM UIDs, filesystem paths never crossing to the browser, including in error states) is **no longer an open gap** — `docs/pii-boundary-safety.md` (the risk register, now a remediation record — every row marked fixed) and `docs/plans/pii-boundary-test-suite.md` (the implementation plan that closed them, `.github/workflows/test.yml` now CI-gating the result) describe what shipped and why; kept here as the reference for "why does this redaction call/field exist" questions, the same role `safety-plan.md` plays for the export-governance work above.

**Frontend rewrite (`frontend_fastapi/`, tracked in `docs/plans/frontend-rewrite-implementation-plan.md`):**
- Phase 0–5 built (scaffolding, `accounts/`, `research_projects/`, `jobs/` incl. combined import→export, admin dashboard + notifications, cutover). `frontend_fastapi/` is now the production frontend and the backend's sole caller.
- Remaining: Phase 6 (decommission `frontend/` — remove Django-specific dependencies, `db.sqlite3`, `manage.py`, the Django migrations directory — once a burn-in period passes with no rollback needed). A cohort/data-availability browser was previously scoped in this plan as Phase 3b; it's been removed and is being planned separately.
- `frontend/` (Django) is kept running during the burn-in period for side-by-side comparison/rollback only — not real traffic. Root `docker-compose.yml` (the actual production compose file) has no `frontend`/`frontend_fastapi` service of its own; production traffic routing for whichever frontend is live is handled outside this repo's tracked infra-as-code.

**Backend / data pipeline:**
- Raystation import not implemented
- Metadata editing ("Modify") not implemented anywhere
- ProKnow RTSTRUCT UID regeneration workaround incomplete (`export/logic.py`)
- CBCT export and "all images" export option pending
- Per-user export destination allow-list not built (`docs/plans/safety-plan.md` §A) — any active project member can currently target any registered Orthanc modality or ProKnow collection; no code distinguishes an anonymising destination from an ordinary clinical one
- `PinnacleExport`'s own internals haven't been audited beyond the two call sites `retrieve/logic.py` already used (SQL injection fix, env-configurable push destination) — worth a follow-up look at whether it has its own persistence that should eventually join HermesDB
- Task queue retries ship disabled (`max_attempts` defaults to `1`) — enabling retries is a deliberate separate decision, not automatic from the queue existing (`docs/plans/worker-queue-design.md`)
- `Home.py` (Streamlit, orphaned — no `pages/` under it any more) and `gateway/anon.py` (a single leftover file from the deleted `gateway/` service) are dead weight not yet removed; the rest of the pre-2026-07-30 Streamlit/`gateway/` stack is already gone and would need rebuilding from scratch if PACS-comparison querying is ever wanted again

**Governance / access control (accepted, not code gaps — see `docs/known-issues.md`):**
- Project approval is a single yes/no gate on tool access, not a bound on which patients or how much volume a member may export — no cohort/volume scoping exists or is currently planned beyond the allow-list above
- Admin/superuser access isn't restricted at the code level (Django superuser auto-enrolled in a bypass project) — accepted as an operational control (small, vetted admin group) rather than a code change
- No pre-flight review before a combined import+export job — can't confirm what's about to be sent until import has actually finished finding it; deferred, revisit if it becomes worth solving

**Infrastructure:**
- The coarse ethics gate (`backend/src/projects/enforcement.py`) is enforced by the backend, but the backend has no auth of its own — `HERMES_INTERNAL_KEY` is the only thing standing between "topologically only the frontend calls this" and "actually enforced"; it's optional (unset → no-op) and should be treated as required, not optional, for any deployment where the backend is reachable from outside the secured network
- A root `requirements.txt`/`requirements-dev.txt` now exists and covers backend + `frontend_fastapi/` + the still-present Streamlit remnants — keep it in sync as dependencies change; there's no per-component `pyproject.toml` pinning beyond `proxy/` and `webui/`
- `docker-compose.dev.yml` + `Dockerfile.dev` bring up the full stack locally (both Postgres DBs, `backend`, `worker`, `frontend_fastapi`, `frontend`) — `frontend_fastapi` on the primary port as the production frontend, `frontend` alongside it only for the Phase 6 burn-in period — see that file's comments for port mapping and seeded dev users
