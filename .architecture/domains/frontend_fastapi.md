---
generated_at: 2026-09-10T22:30:00Z
staleness_key: git:2a963abbdbf5836ca03a46c7a49d9dafa915c8c4e59edd808696667ea2bfa2ba
generated_at_commit: dc26003b806cdecbeaa59369e40f7b34189bbe0e
parent: ../../ARCHITECTURE.md
---

# Domain: frontend_fastapi

> Sub-map of the top-level [ARCHITECTURE.md](../../ARCHITECTURE.md), split out because inlining its full detail would have pushed the top-level map past its size budget.

The production frontend (FastAPI + Jinja2) as of the Phase 5 cutover — sole caller of `backend/` for real traffic. Has its own small local database (users/sessions/`ProjectDocument`s only); all job/event/research-project data is fetched fresh from the backend via `backend_client.py`.

## Structure

```mermaid
graph TD
    MAIN[main.py\nlifespan: migrations, DB pool]
    ACCOUNTS[routers/accounts.py\nlogin, invite, activate]
    PROJECTS[routers/research_projects.py\nethics workflow]
    JOBS[routers/jobs.py\nsubmit/watch/results, SSE relay]
    ADMIN[routers/admin.py\ncompliance dashboard]
    NOTIFS[routers/notifications.py]
    ERRORREPORTS[routers/error_reports.py]
    LOCALPACS[routers/local_pacs.py\nbrowse, destinations CRUD, move]
    CONQUEST[local_pacs/conquest_client.py\npynetdicom: C-ECHO/C-FIND/C-MOVE]
    CLIENT[backend_client.py\nsole caller of backend/]
    LOCALDB[(local DB\nusers, sessions, project_documents,\nlocal_pacs_destinations)]

    MAIN --> ACCOUNTS
    MAIN --> PROJECTS
    MAIN --> JOBS
    MAIN --> ADMIN
    MAIN --> NOTIFS
    MAIN --> ERRORREPORTS
    MAIN --> LOCALPACS
    ACCOUNTS --> LOCALDB
    ACCOUNTS --> CLIENT
    PROJECTS --> CLIENT
    JOBS --> CLIENT
    ADMIN --> CLIENT
    NOTIFS --> CLIENT
    ERRORREPORTS --> CLIENT
    LOCALPACS --> LOCALDB
    LOCALPACS --> CONQUEST
    LOCALPACS --> CLIENT
    CONQUEST -.DICOM, not HTTP.-> CQ[(Conquest PACS\nexternal, anonymised-only)]
```

## Key paths

| Component | Path | Description |
|---|---|---|
| App entrypoint | `frontend_fastapi/main.py` | Lifespan runs local-DB migrations, opens the shared `httpx.AsyncClient` `backend_client.py` uses |
| Backend client | `frontend_fastapi/backend_client.py` | The ONLY module here that talks to `backend/`; attaches `X-Hermes-Internal-Key` + the session's own username, never a browser-supplied value |
| Sessions/CSRF/auth | `frontend_fastapi/session_middleware.py`, `deps.py`, `auth.py`, `security.py`, `exceptions.py` | Hand-rolled DB-backed sessions (CSRF token + flash messages live on the session row); `NotAuthenticated`/`Forbidden` auth gates; `deps.py`'s `get_template_context` also assembles every staff-only live nav badge count (active-project/expiring-soon, review-queue-pending, unaddressed-urgent-error-reports), each degrading to 0/absent on a backend error rather than failing the page |
| Accounts | `frontend_fastapi/routers/accounts.py`, `forms/accounts.py` | Login, invite, create-user, activate; break-glass scripts: `scripts/reset_password.py`, `scripts/set_staff.py` |
| Research projects | `frontend_fastapi/routers/research_projects.py`, `forms/research_projects.py` | Ethics-project workflow (create/submit/review queue/detail); `models.py`'s `ProjectDocument` is the one HERMES-specific local model (ethics-certificate uploads) |
| Jobs | `frontend_fastapi/routers/jobs.py`, `forms/jobs.py` | Single/batch import, DICOM/ProKnow export (incl. combined import→export), results lookup, job-detail patient drill-down, `job_stream`'s SSE re-framing of the backend's observer stream |
| Admin | `frontend_fastapi/routers/admin.py` | Compliance dashboard, gated by `require_data_custodian`; also the show/hide-addressed toggle and mark-addressed POST action for `error_reports` (`POST /admin/error_reports/{id}/resolve`) |
| Notifications | `frontend_fastapi/routers/notifications.py` | Persisted job-done/approval-decision notifications |
| Error reports | `frontend_fastapi/routers/error_reports.py`, `forms/error_reports.py` | Submission form for the backend's `error_reports` log |
| Local PACS (Conquest) | `frontend_fastapi/local_pacs/conquest_client.py`, `routers/local_pacs.py`, `forms/local_pacs.py` | The only DICOM integration in this codebase using raw `pynetdicom` (not Orthanc's REST API) — `backend`/Orthanc can't reach Conquest (firewalled), so this connection lives entirely here. `conquest_client.py`: C-ECHO/C-FIND/C-MOVE with distinguishable `ConquestError` subclasses; `routers/local_pacs.py`: login-gated browse/search (`/local_pacs`) + series drill-down partial + an htmx-loaded `GET /local_pacs/status` connectivity indicator (auto C-ECHO check shown before any search runs), `require_data_custodian`-gated destination CRUD (`models.py`'s `LocalPacsDestination`) and both a single-study move and a multi-study batch move (checkboxes + one destination, `POST /local_pacs/move_batch`). Every move calls `backend_client.audit_local_pacs_move` afterward (success or failure, once per study even in a batch) so it lands in HermesDB's audit trail even though the DICOM transfer itself never touches `backend`. The batch move's `GET .../watch` + `GET .../stream` pair is this app's only genuine local SSE *producer* (every other SSE view, `jobs.py`'s `job_stream`, relays a backend-generated stream) — a background `asyncio.create_task` runs the moves independently of whether the stream is being watched, writing into a module-level, in-memory `_BATCHES` dict (this app's only ephemeral cross-request process state) that a polling stream reader replays from the start on every connection |
| Templates | `frontend_fastapi/templates/`, `templating.py` | Jinja2; `templating.py` holds the single shared `Jinja2Templates` instance + custom filters (`hermes_timestamp`/`hermes_date`) and the `is_project_expired` global, applied throughout so no template renders a raw ISO timestamp; `templates/base.html` is the shared layout (incl. the live nav badges above), `templates/jobs/dashboard.html` ("Home") the frontpage recent-jobs table, `templates/research_projects/_macros.html`'s `project_table` the shared table-row macro for the projects list/review-queue pages |
| Local DB / migrations | `frontend_fastapi/models.py`, `database.py`, `migrations.py`, `alembic/` | `HERMES_FRONTEND_DATABASE_URL` (defaults to local SQLite); migrations run automatically on startup, same pattern as the backend |

## Test organisation

`frontend_fastapi/tests/` (31 files), pytest, own local DB (SQLite in-memory or throwaway Postgres). `backend_client`'s functions are monkeypatched with `AsyncMock` across most router tests (see `test_jobs.py`/`test_research_projects.py`'s `mock_backend` fixture pattern) rather than hitting a real backend. `test_conquest_client.py` is the one exception to "no live protocol calls" — it runs a hand-rolled `pynetdicom` test SCP in-process rather than mocking the DICOM library itself. `test_local_pacs_batch_move.py` also awaits `_run_batch`/`_stream_batch_events` directly (mirroring `backend/tests/test_observer_stream.py`'s approach to testing an async generator) rather than racing a real background task through the TestClient.

## Notable conventions

- Project selection has no implicit/session concept — every submission form carries its own `project_id`, populated fresh from `backend_client.list_user_active_projects` on every request; the form's own validation against those live choices is the re-check that the submitted `project_id` is still one the user has active access to.
- Every batch job is enqueued directly onto the backend's task queue from the submit route — nothing is staged to local disk or session first, so `job_watch`/`job_stream` re-check live visibility on every request rather than trusting anything cached from submission time.
