---
generated_at: 2026-09-10T00:00:00Z
staleness_key: git:940f4d6c867ecc17fbbafea3c8697e36289d66c7ee9d5b74788fd437d1701d83
generated_at_commit: 708cde90229e9a825ff24d02f3498e6271a48be1
parent: ../../ARCHITECTURE.md
---

# Domain: backend

> Sub-map of the top-level [ARCHITECTURE.md](../../ARCHITECTURE.md), split out because inlining its full detail would have pushed the top-level map past its size budget.

One FastAPI app (`backend/main.py`) holding every feature; `backend/worker.py` is a separate, plain-synchronous process claiming rows from the `tasks` table and calling the same worker factories the still-synchronous endpoints use.

## Structure

```mermaid
graph TD
    MAIN[main.py]
    RETRIEVE[retrieve/\nimport, PinnacleExport submodule]
    EXPORT[export/\nDICOM C-MOVE, ProKnow upload]
    RESULTS[results/\njob/patient lookups, observer SSE stream]
    STUDIES[studies/\nread-only Orthanc /tools/find]
    PROJECTS[projects/\nethics workflow + enforcement gate]
    STATUS[status/\nStatusDB, tasks queue, hash chain]
    IDENTITY[identity/\nanon <-> real id boundary]
    NOTIFICATIONS[notifications/]
    ADMIN[admin/\ncompliance dashboard]
    PLANS[plans/\nread-only PinnacleExport plans table]
    COMMON[common/\nSSE runner, PII redaction, errors]

    MAIN --> RETRIEVE
    MAIN --> EXPORT
    MAIN --> RESULTS
    MAIN --> STUDIES
    MAIN --> PROJECTS
    MAIN --> ADMIN
    MAIN --> NOTIFICATIONS
    RETRIEVE --> STATUS
    EXPORT --> STATUS
    RETRIEVE --> IDENTITY
    EXPORT --> IDENTITY
    STUDIES --> IDENTITY
    RESULTS --> IDENTITY
    RETRIEVE --> PROJECTS
    EXPORT --> PROJECTS
    RESULTS --> COMMON
    RETRIEVE --> COMMON
```

## Key paths

| Component | Path | Description |
|---|---|---|
| App entrypoint | `backend/main.py` | Registers every router; runs Alembic migrations + pool init on startup |
| Worker process | `backend/worker.py` | `python -m backend.worker`; polls `tasks`, no business logic of its own |
| Import | `backend/src/retrieve/logic.py`, `backend/src/retrieve/endpoints.py` | `Importer` class (Mosaiq/Pinnacle/ProKnow search, Orthanc pull, `_cleanup_orthanc`); `PinnacleExport/` git submodule lives under `retrieve/` |
| Export | `backend/src/export/logic.py`, `backend/src/export/endpoints.py` | `Exporter` class: C-MOVE or ProKnow SDK upload |
| Results | `backend/src/results/endpoints.py` | Job/patient lookups, `GET /results/job/{job_id}/stream` observer stream, `_scrub`/`_scrub_json` PII redaction on outbound data |
| Studies | `backend/src/studies/endpoints.py` | Read-only Orthanc `/tools/find` browsing |
| Project/ethics workflow | `backend/src/projects/db_client.py`, `endpoints.py`, `enforcement.py` | `ProjectsDB` (create/submit/review/revoke, membership, audit log); `enforcement.py`'s `require_project_member`/`require_any_active_project`/`verify_internal_key` gate every import/export/project call |
| Status/audit | `backend/src/status/db_client.py` (`StatusDB`), `tasks_db.py` (`TasksDB`), `hash_chain.py`, `audit_chain_db.py` | Job/patient/event tracking, the `tasks` queue (Postgres `SELECT...FOR UPDATE SKIP LOCKED`), the tamper-evident `events` hash chain |
| Anonymisation boundary | `backend/src/identity/anon.py` | `resolve_real_id`/`to_display_id`, `shift_date`; passthrough when `ANON_DB_*`/`ANON_CONFIG` unset |
| Plans | `backend/src/plans/db_client.py` (`PlansDB`) | Read-only access to PinnacleExport's own `plans` table (same Postgres database, separate schema) |
| Notifications | `backend/src/notifications/` | Persisted job-done/approval-decision notifications |
| Admin | `backend/src/admin/endpoints.py` | Compliance dashboard: project-status counts, expiring-soon list, recent-jobs-with-counts table, audit-chain status |
| Shared infra | `backend/src/common/sse.py`, `pii_patterns.py`, `errors.py` | `run_batch_job`/`BatchItem` SSE generator; `redact`/`redact_dict` PII floor; global PII-safe exception handler |
| DB pool / migrations | `backend/src/db.py`, `backend/src/database.py`, `backend/alembic/versions/` | Shared `psycopg2` pool (`DATABASE_URL`); Alembic runs on startup |

## Test organisation

`backend/tests/` (38 files) — pytest against a real Postgres, no mocked DB layer. Notable groupings: `test_status_db.py`/`test_projects_db.py`/`test_projects_enforcement.py`/`test_tasks_db.py`/`test_worker.py`/`test_observer_stream.py`/`test_hash_chain.py` (core data-layer + queue behaviour); `test_*_anon_boundary.py` + `test_*_pii_boundary.py` + `backend/tests/support/pii_assertions.py` (the PII-boundary-specific suite, built directly on `pii_patterns.py`); `test_cleanup_orthanc.py`/`test_retrieve_endpoints_errors.py` need the `PinnacleExport` submodule and `pytest.importorskip` if it's absent. `conftest.py`'s `active_project` fixture creates a fully-approved project for tests that need to pass the ethics gate.

## Notable conventions

- `tasks` (mutable queue state) and `events` (immutable audit log) are deliberately separate tables — same mutable/immutable split chosen for `research_projects` vs `project_audit_log`.
- Every outbound response/SSE event goes through the anonymisation boundary (`identity/anon.py`) plus the free-text PII floor (`common/pii_patterns.py`) — see `docs/pii-boundary-safety.md` before touching any new outbound endpoint.
