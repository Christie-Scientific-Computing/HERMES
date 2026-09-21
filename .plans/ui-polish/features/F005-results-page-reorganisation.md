# Feature F005: Results page reorganisation

## Purpose

The Results page (`/results`) currently shows an unbounded "Your jobs" table first, followed by the
job/patient search below it — the reverse of what most visits to this page are probably for (looking
something specific up), and with no cap on the jobs table means a user or admin with a long project history
sees an ever-growing table with no limit. This feature reorders the page so search comes first, and brings
the jobs table's format and size cap in line with the Home page's existing recent-jobs table.

## Behaviour

- The "Narrow your search" section (the by-job / by-patient tabs and their forms/results) moves to the top
  of the page, above "Your jobs".
- "Your jobs" (renamed or not — implementer's call, currently titled "Your jobs (N)") shows at most 25 rows,
  sorted the same way as today (most recent first, per the existing `created_at` descending sort in
  `results_lookup`).
- Its columns switch from the current (Job / Project / Description / Created) to match the Home page's
  table format: Description / Created / Imported / Exported (the counts columns), reusing
  `backend_client.list_project_jobs_with_counts` instead of `list_project_jobs`, the same call the Home page
  dashboard already uses. The existing **Job** (id) and **Project** columns are kept in addition — Results
  spans multiple projects the way Home's table also technically can, but Home doesn't show a Project column
  today; removing it from Results wasn't requested and it's useful information already shown today, so it's
  kept (see `plan.md`'s Assumptions).
- The heading continues to show a count, following the existing "N total — showing M" convention already
  used elsewhere on this same page (the patient-table summary section: `Patients ({{ total }}) —
  showing {{ rows|length }}`), so a user with more than 25 jobs can tell there are more than what's shown.

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/templates/jobs/results_lookup.html` — the whole page: "Your jobs" table first, then
  "Narrow your search" (tabs + forms + per-lookup results) below it.
- `frontend_fastapi/templates/jobs/dashboard.html` — the table format to match (Description / Created /
  Imported / Exported columns, `min-w-full text-sm divide-y divide-gray-100` table styling).
- `frontend_fastapi/routers/jobs.py:74-86` (`dashboard`) — `list_project_jobs_with_counts` usage +
  `jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)` + `jobs[:10]` slicing, the exact
  pattern to replicate with a cap of 25 instead of 10.
- `frontend_fastapi/routers/jobs.py:460-474` (`results_lookup`) — currently builds `project_jobs` via
  `backend_client.list_project_jobs` (no counts) across `_users_projects(user)`; this is the loop to switch
  to the counts-returning call, keeping `_users_projects` (not `_project_choices_for`) since results
  intentionally shows jobs from projects of any status, per that function's own existing docstring.

### Likely changes

- `frontend_fastapi/routers/jobs.py`'s `results_lookup` — switch to `list_project_jobs_with_counts`, keep
  `project_title` merged in the same way it is today (`{**j, "project_title": p["title"]}`), sort as today,
  slice to `[:25]`, keep the total pre-slice count available to the template for the "showing M of N"
  message.
- `frontend_fastapi/templates/jobs/results_lookup.html` — reorder the two top-level sections; update the
  jobs table's columns/heading.

## Acceptance criteria

### Scenario: Search appears above the jobs table

**Given** a logged-in user visits `/results`

**When** the page renders

**Then** the "Narrow your search" tabs/forms appear before the "Your jobs" table in the page's source order

### Scenario: Jobs table is capped at 25

**Given** a user belongs to projects with 40 jobs total across them

**When** they visit `/results`

**Then** the "Your jobs" table shows exactly 25 rows (the 25 most recent by `created_at`), and the heading
indicates there are more than what's shown (e.g. "Your jobs (40) — showing 25"), mirroring this same page's
existing "Patients (total) — showing X" convention

### Scenario: Fewer than 25 jobs shows all of them, no truncation message

**Given** a user belongs to projects with 8 jobs total

**When** they visit `/results`

**Then** all 8 rows show, and no "showing M of N" qualifier appears (matching the existing conditional
`{% if rows|length != total %}` pattern already used elsewhere on this page)

### Scenario: Table shows import/export counts like the Home page

**Given** a job has been partially imported and exported

**When** it appears in the "Your jobs" table

**Then** its row shows Imported and Exported count columns in the same `N / M` format the Home page's
table already uses, in addition to the existing Job and Project columns

### Scenario: Search results below still work after reordering

**Given** a user submits the by-job search form

**When** the page reloads with `?lookup=job&job_id=...`

**Then** the search form (now at the top) retains its submitted state and the summary/patient-table results
still render below it, functionally unchanged from today — only position on the page changed

## Edge cases

- A user with zero jobs: unchanged existing empty-state message ("No jobs under any of your projects yet."),
  just relocated below the (now-first) search section.
- Staff users searching a job/patient outside their own projects: unaffected by this feature — that
  behaviour lives entirely in the search forms' backend calls below the "Your jobs" table, which this
  feature doesn't change.

## Success

Visiting `/results` puts the search controls in front, and "Your jobs" reads the same way the Home page's
recent-jobs table does, capped so it can't grow unbounded for a long-lived project.

## Failure behaviour

Unchanged from today: if `backend_client.list_project_jobs_with_counts` fails for a given project, that
project's jobs are simply skipped from the aggregate list (per the existing `try/except
backend_client.BackendError: pass` in the loop) rather than failing the whole page.

## Testing considerations

`frontend_fastapi/tests/test_jobs.py` already covers `results_lookup` (per `CLAUDE.md`'s testing
conventions and the file's existing scope) — extend with: a case asserting more than 25 jobs across mocked
projects renders only 25 rows sorted by `created_at` descending, a case asserting the Imported/Exported
columns appear with counts data, and (if page structure is asserted anywhere) that the search section's
markup precedes the jobs table's markup in response body order.

## Implementation notes

- Follow the Home page's `dashboard` handler as the reference implementation for the counts-table sourcing
  and sort/slice logic — this feature is explicitly about reusing that existing pattern, not inventing a
  new one.
- `list_project_jobs_with_counts` takes a `limit` parameter per-project-call (default 10) — when aggregating
  across multiple projects before the final 25-row slice, pass a high-enough per-call limit (or rely on the
  existing per-project default and top up if needed) so a user with jobs concentrated in one project doesn't
  lose real recent jobs to a too-low per-call limit before the final sort/slice happens; mirror whatever
  `dashboard` already does here (it currently relies on the default `limit=10` per project, which the same
  caveat already applies to today — worth a slightly higher shared constant if it's cheap to bump, but not
  a hard requirement to change `dashboard`'s own behaviour as part of this feature).

## Out of scope

- Changing the by-job/by-patient search forms' own behaviour or the timeline/summary tables below them
  (only their position on the page changes).
- Adding pagination beyond the 25-row cap.
