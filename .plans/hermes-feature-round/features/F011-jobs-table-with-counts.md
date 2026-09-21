# Feature F011: Jobs table with counts on the frontpage

## Purpose

The frontpage jobs table shows a raw job UUID and a description that, for
batch imports, leaks a server temp filepath — cluttered and not useful for
deciding which job to look at. This feature replaces it with a cleaner
description and the same import/export counts already shown on the admin
dashboard's richer, near-identical table.

## Behaviour

The frontpage's "recent jobs" table drops the raw job-ID column and shows: a
cleaned, human-readable description (no filepath), Created, and
Imported/Exported counts — scoped to the viewer's own projects, same rows as
today.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/routers/jobs.py` (`dashboard`, ~lines 74-86, route name
  `dashboard`, mounted at `GET /`) — calls
  `backend_client.list_project_jobs(project_id)` per project, merges/sorts
  by `created_at`, takes top 10.
- `frontend_fastapi/templates/jobs/dashboard.html` (~lines 28-39) — current
  columns: Job (raw `job.job_id` UUID, linked), Description, Created.
- `backend/src/projects/db_client.py` (`list_project_jobs`, ~lines 270-276)
  — bare `SELECT * FROM jobs WHERE project_id = %s`, the source of the raw,
  count-less rows.
- `backend/src/retrieve/endpoints.py` (~line 299) — the root cause of the
  filepath leak: `description=f"Batch import from {tmp_path}"`, where
  `tmp_path = Path("./tmp") / f"{job_id}_{file.filename}"` (~line 293).
  Single-import jobs already get a clean `f"Single import ({level})"`
  (~line 143); only the batch-import path is affected.
- `backend/src/status/db_client.py` (`list_recent_jobs_with_counts`, ~lines
  131-152) — the existing richer query (Job, Created by, Created,
  imported/submitted, exported/export_attempted), already used by the admin
  dashboard (`frontend_fastapi/routers/admin.py:28`,
  `templates/admin/overview.html:71-92`). This feature scopes the same query
  per-project instead of global.

### Likely changes

- `backend/src/retrieve/endpoints.py` — fix the description string to use
  `file.filename` only, e.g. `f"Batch import ({file.filename})"`, dropping
  `tmp_path` from the description entirely (the temp file itself is
  untouched — only the description text changes).
- `backend/src/status/db_client.py` — add a `project_id` filter parameter to
  `list_recent_jobs_with_counts` (or a small dedicated variant), so it can
  be scoped per-project rather than only globally.
- `frontend_fastapi/routers/jobs.py`'s `dashboard` view — call the
  counts-aware, project-scoped query instead of `list_project_jobs`.
- `frontend_fastapi/templates/jobs/dashboard.html` — new columns:
  Description | Created | Imported | Exported.

## Acceptance criteria

### Scenario: Filepath no longer appears in new job descriptions

**Given** a user submits a new batch import job with file `patients.csv`

**When** the job is created

**Then** its `description` is `"Batch import (patients.csv)"`, containing no
temp directory path or job UUID

### Scenario: Frontpage shows counts, not a raw UUID

**Given** a project with jobs that have both succeeded and partially failed
imports

**When** a member views the frontpage jobs table

**Then** each row shows a description, created timestamp, and
Imported/Exported counts (e.g. "42/50", "38/38"), with no raw job UUID
displayed as visible text

### Scenario: Table stays scoped to the viewer's own projects

**Given** a user who is a member of some but not all projects in the system

**When** they view the frontpage

**Then** only jobs from their own projects appear, exactly as today's
`list_project_jobs`-based behaviour already scopes them

## Edge cases

- Existing jobs created before this fix still carry the old, filepath-laden
  description in the database — this feature does not retroactively rewrite
  historical `description` values; only newly created jobs get the cleaned
  text. Decide whether that's acceptable (recommended: yes — no requirement
  was given to backfill historical data, consistent with F005's "leave
  existing rows as-is" precedent).
- A job with zero imports/exports attempted yet (just created, nothing
  processed) — counts should show `0/0` or similar, not error or omit the
  row.

## Success

A freshly submitted batch import job's description contains no filepath;
the frontpage table for a project with real job history shows the same
Imported/Exported figures the admin dashboard's table would show for those
same jobs, scoped to the viewer's projects.

## Failure behaviour

N/A beyond standard query-failure handling already in place for the
existing dashboard route.

## Testing considerations

- Backend: extend whatever test covers `batch_import_file`'s description
  construction (or add one) to assert the new format contains no path
  separator/temp-directory fragment.
- Backend: extend `backend/tests/` coverage for
  `list_recent_jobs_with_counts` (or its new variant) with a `project_id`
  filter case.
- Frontend: `frontend_fastapi/tests/test_jobs.py` — assert the dashboard
  renders the new columns and that jobs from other projects don't appear.

## Implementation notes

This fix is independent of every other feature in this round and can be
built first as a small warm-up PR if a quick, low-risk change is wanted
before tackling the larger schema-touching features.

## Out of scope

- Rewriting historical job descriptions already stored with the old,
  filepath-laden text.
- Any change to the admin dashboard's own existing table (already correct).
