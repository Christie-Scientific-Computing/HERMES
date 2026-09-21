# Feature F008: Project overview stats

## Purpose

A project's detail page today shows approval status but nothing about
actual activity — how many patients were requested, how many were
successfully restored, or where data has actually been sent. Reviewers and
project owners have no at-a-glance answer to "is this project doing what it
was approved to do." This feature adds that summary, plus makes the
project's expiry date visually impossible to miss when it's approaching.

## Behaviour

The project detail page's "Approval status" card gains a sibling "Project
overview" card showing three figures: **Requested** (from F007's uploaded
list), **Restored** (patients successfully imported for this project's
jobs), and **Sent to <destination>** (one line per destination the project
has actually exported to, each counting distinct patients). The existing
expiry-date banner becomes colour-coded: a stronger warning colour the
closer the project is to (or past) its expiry date.

## Dependencies

- **F006 — Approved export destinations & message ID**: "Sent to
  <destination>" needs the project's destinations to exist and be
  queryable.
- **F007 — Optional patient-ID-list upload**: "Requested" needs
  `project_requested_patients` to exist.

Ships only once both are in place — no interim placeholder version.

## Relevant code

### Existing

- `frontend_fastapi/routers/research_projects.py` (`project_detail`, ~lines
  148-187) — currently passes `project`, `is_member`, `documents`, `jobs`,
  `days_remaining`; no stats computed today.
- `frontend_fastapi/templates/research_projects/detail.html` — "Approval
  status" card (~lines 27-51, just the `timeline_stepper` macro + buttons);
  the existing `days_remaining` banner (~lines 16-20, plain, not
  colour-coded) — the card to add colour to; "Job history" table (~lines
  87-109) lists `job_id`/`description`/`created_at` only, no counts.
- `frontend_fastapi/deps.py` (~lines 27-60) — `EXPIRING_SOON_WITHIN_DAYS =
  30`, `expiring_soon(active_projects, within_days=30)`. `project_detail`
  already calls this for its own `days_remaining` banner — the source for
  this feature's colour thresholds.
- `backend/src/status/db_client.py` (`list_recent_jobs_with_counts`, ~lines
  131-152) — existing JOIN+GROUP BY pattern computing
  `submitted_count`/`imported_count`/`exported_count`/`export_attempted_count`
  per job, already selecting `j.project_id`. The pattern to extend into a
  per-project aggregate rather than per-job.
- `backend/src/projects/endpoints.py` (`GET /projects/{project_id}/jobs`,
  ~lines 158-159) and `backend/src/projects/db_client.py`
  (`list_project_jobs`, ~lines 270-276) — existing bare `SELECT * FROM jobs
  WHERE project_id = %s`, no counts; the endpoint this feature's new stats
  endpoint sits alongside.
- `events.details` JSONB — where export destination is assumed recorded per
  event (`details->>'destination'`); **verify this before implementing the
  per-destination GROUP BY** (see Open issues in `plan.md`).

### Likely changes

- New backend query/method (`ProjectsDB` or `StatusDB`) and endpoint `GET
  /projects/{project_id}/stats` returning:
  - `requested`: `COUNT(*) FROM project_requested_patients WHERE project_id
    = %s` (from F007).
  - `restored`: distinct-MRN count of successful `retrieve` events for the
    project's jobs (same predicate `list_recent_jobs_with_counts` already
    uses for `imported_count`, scoped by `jobs.project_id`).
  - `sent`: one row per destination — distinct-MRN count of successful
    `export` events, grouped by destination, scoped to the project's
    `project_destinations` (F006).
- `frontend_fastapi/routers/research_projects.py` — `project_detail` calls
  the new endpoint.
- `frontend_fastapi/templates/research_projects/detail.html` — new "Project
  overview" card beside "Approval status"; expiry banner gains a colour tier
  (e.g. neutral beyond 30 days, amber within 30, red within 7 or past due —
  exact cutovers are an implementation detail, not a re-confirmation point).

## Acceptance criteria

### Scenario: All three stats display correctly

**Given** a project with 50 requested patients, 42 successfully restored,
and exports to two destinations (30 unique patients to destination A, 12 to
destination B)

**When** a member views the project detail page

**Then** the overview card shows "Requested: 50", "Restored: 42", "Sent to
A: 30", "Sent to B: 12"

### Scenario: Sent-to-destination counts unique patients, not events

**Given** a single patient exported twice to the same destination (e.g. a
retry)

**When** viewing the project overview

**Then** that patient counts once toward that destination's total, not twice

### Scenario: Expiry colour reflects proximity

**Given** a project with an `expiry_date` 5 days away

**When** viewing the project detail page

**Then** the expiry indicator renders in its most urgent colour tier (worse
than a project 20 days from expiry, which renders in a milder tier)

### Scenario: No requested-patient list uploaded

**Given** a project created without F007's optional upload

**When** viewing the project overview

**Then** "Requested" shows `0` (or an explicit "not provided" label,
distinguishable from "zero uploaded but tracked") rather than erroring

## Edge cases

- A project with destinations but zero exports yet — "Sent to
  <destination>" should show `0` for each active destination, not omit the
  line entirely, so a reviewer can see nothing has shipped yet.
- A project whose destinations changed via an approved amendment (F006) —
  "Sent to <destination>" should reflect only the *currently active*
  destinations; historical exports to a since-removed destination are a
  reasonable thing to still show under that destination's (now-inactive)
  label, or to omit — pick the simpler option (show only active
  destinations) unless the implementer finds a strong reason otherwise.

## Success

A project detail page for a project with real job/event history displays
the three stats matching a manual count against the raw `events`/
`project_requested_patients` data, and the expiry banner's colour visibly
changes as the `expiry_date` fixture moves across the threshold boundaries.

## Failure behaviour

If the new stats endpoint fails (e.g. backend error), the project detail
page should still render everything else (approval status, job history) —
the overview card can show a "stats unavailable" state rather than taking
the whole page down, consistent with `PlansDB.list_plans_for_patient`'s
existing pattern of returning `None` to distinguish "unavailable" from
"zero" elsewhere in this codebase.

## Testing considerations

- Backend: new tests alongside `backend/tests/test_projects_db.py`/
  `test_status_db`-equivalent, covering the three stat computations against
  seeded `events`/`tasks`/`project_requested_patients`/`project_destinations`
  fixtures, including the distinct-patient-count case for "sent".
- Frontend: `frontend_fastapi/tests/test_research_projects.py` — assert the
  overview card renders the three stats and the expiry banner's colour class
  changes across threshold fixtures.

## Implementation notes

Confirm the exact JSONB path for export destination on an `events` row
before writing the `GROUP BY` (see `plan.md`'s Open issues) — this plan
assumes `details->>'destination'` based on `redact_dict`'s
`NON_PII_STRUCTURAL_FIELDS` list in `CLAUDE.md`, which names `destination`
as a passthrough field, but verify against the actual write path in
`export/endpoints.py` before relying on it.

## Out of scope

- Historical trend/time-series views of these stats — a point-in-time
  snapshot is all that's requested.
- Any change to job history table shown lower on the same page.
