---
generated_at: 2026-09-10T00:00:00Z
staleness_key: git:940f4d6c867ecc17fbbafea3c8697e36289d66c7ee9d5b74788fd437d1701d83
generated_at_commit: 708cde90229e9a825ff24d02f3498e6271a48be1
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
    CLIENT[backend_client.py\nsole caller of backend/]
    LOCALDB[(local DB\nusers, sessions, project_documents)]

    MAIN --> ACCOUNTS
    MAIN --> PROJECTS
    MAIN --> JOBS
    MAIN --> ADMIN
    MAIN --> NOTIFS
    ACCOUNTS --> LOCALDB
    ACCOUNTS --> CLIENT
    PROJECTS --> CLIENT
    JOBS --> CLIENT
    ADMIN --> CLIENT
    NOTIFS --> CLIENT
```

## Key paths

| Component | Path | Description |
|---|---|---|
| App entrypoint | `frontend_fastapi/main.py` | Lifespan runs local-DB migrations, opens the shared `httpx.AsyncClient` `backend_client.py` uses |
| Backend client | `frontend_fastapi/backend_client.py` | The ONLY module here that talks to `backend/`; attaches `X-Hermes-Internal-Key` + the session's own username, never a browser-supplied value |
| Sessions/CSRF/auth | `frontend_fastapi/session_middleware.py`, `deps.py`, `auth.py`, `security.py`, `exceptions.py` | Hand-rolled DB-backed sessions (CSRF token + flash messages live on the session row); `NotAuthenticated`/`Forbidden` auth gates |
| Accounts | `frontend_fastapi/routers/accounts.py`, `forms/accounts.py` | Login, invite, create-user, activate; break-glass scripts: `scripts/reset_password.py`, `scripts/set_staff.py` |
| Research projects | `frontend_fastapi/routers/research_projects.py`, `forms/research_projects.py` | Ethics-project workflow (create/submit/review queue/detail); `models.py`'s `ProjectDocument` is the one HERMES-specific local model (ethics-certificate uploads) |
| Jobs | `frontend_fastapi/routers/jobs.py`, `forms/jobs.py` | Single/batch import, DICOM/ProKnow export (incl. combined import→export), results lookup, job-detail patient drill-down, `job_stream`'s SSE re-framing of the backend's observer stream |
| Admin | `frontend_fastapi/routers/admin.py` | Compliance dashboard, gated by `require_data_custodian` |
| Notifications | `frontend_fastapi/routers/notifications.py` | Persisted job-done/approval-decision notifications |
| Templates | `frontend_fastapi/templates/` | Jinja2; `templates/base.html` is the shared layout, `templates/jobs/dashboard.html` the frontpage recent-jobs table |
| Local DB / migrations | `frontend_fastapi/models.py`, `database.py`, `migrations.py`, `alembic/` | `HERMES_FRONTEND_DATABASE_URL` (defaults to local SQLite); migrations run automatically on startup, same pattern as the backend |

## Test organisation

`frontend_fastapi/tests/` (24 files), pytest, own local DB (SQLite in-memory or throwaway Postgres). `backend_client`'s functions are monkeypatched with `AsyncMock` across most router tests (see `test_jobs.py`/`test_research_projects.py`'s `mock_backend` fixture pattern) rather than hitting a real backend.

## Notable conventions

- Project selection has no implicit/session concept — every submission form carries its own `project_id`, populated fresh from `backend_client.list_user_active_projects` on every request; the form's own validation against those live choices is the re-check that the submitted `project_id` is still one the user has active access to.
- Every batch job is enqueued directly onto the backend's task queue from the submit route — nothing is staged to local disk or session first, so `job_watch`/`job_stream` re-check live visibility on every request rather than trusting anything cached from submission time.
