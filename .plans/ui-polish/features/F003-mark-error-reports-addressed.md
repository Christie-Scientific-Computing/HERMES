# Feature F003: Mark error reports as addressed

## Purpose

`backend/src/error_reports/` (user-submitted feedback/error reports, with a `category` and an `urgent`
flag) currently has no notion of an admin having dealt with a report — every report submitted ever stays
in the admin page's list forever, with no way to tell what's still outstanding. This feature adds a
persisted "addressed" state, an at-a-glance amber/red indicator for outstanding issues, and moves the
error-reports section to a more prominent position on the admin page.

## Behaviour

- Each error report can be marked "addressed" by a staff user, from the admin overview page. Once marked,
  it records who addressed it and when (mirroring `notifications.read_at`'s shape).
- The admin page's error-reports section, by default, shows only **unaddressed** reports. A toggle/link
  (reusing the existing filter-pill visual convention from `jobs/_macros.html`'s `patient_table`) switches
  to show addressed ones too (or "all" — implementer's choice of exact pill labels, following the existing
  pill pattern of "label (count)").
- The admin page shows a coloured indicator reflecting the current state of *unaddressed* reports:
  - **Amber**, if there are one or more unaddressed reports, none of them urgent.
  - **Red**, if there are one or more unaddressed *urgent* reports.
  - No indicator, if there are zero unaddressed reports.
  This indicator is scoped to the error-reports section on the admin page itself (the nav-level version of
  the red case is F004, a separate feature).
- The error-reports section moves to appear directly below "Expiring soon" and above "Recent jobs" on the
  admin overview page (currently it's last, below "Recent jobs").

## Dependencies

None.

## Relevant code

### Existing

- `backend/src/error_reports/db_client.py` (`ErrorReportsDB`) — `create`/`list_all`; needs a new method to
  mark a report addressed, and `list_all` needs to be able to filter by addressed state.
- `backend/src/error_reports/endpoints.py` — `POST /error_reports` (create), `GET /error_reports` (list);
  needs a new `POST /error_reports/{id}/resolve`-style endpoint, following
  `backend/src/notifications/endpoints.py`'s `POST /notifications/{notification_id}/read` as the direct
  template (same shape: path param id, no body, returns `{"ok": true}` / 404 if nothing matched).
- `backend/src/notifications/db_client.py`'s `mark_read` — the closest existing analogue: an
  `UPDATE ... SET <ts column> = now() WHERE id = %s AND <already-not-set-guard> ... RETURNING`-style
  update, returning whether a row was actually changed.
- `frontend_fastapi/routers/admin.py` (`admin_overview`) — already calls `backend_client.list_error_reports()`
  and passes `error_reports` into the template; needs to also handle the new addressed-toggle query param
  and pass through the new field(s).
- `frontend_fastapi/templates/admin/overview.html` — the four `<div class="bg-white ...">` sections in
  their current order (Projects-by-status/Audit-chain, Expiring soon, Recent jobs, Error reports &
  feedback) — reorder so Error reports sits between Expiring soon and Recent jobs; add the indicator and
  mark-addressed action.
- `frontend_fastapi/templates/jobs/_macros.html`'s `patient_table` pill markup (`?{{ base_query }}filter=...`
  with an active/inactive style) — the convention to follow for the show/hide-addressed toggle.
- `backend/alembic/versions/ddccca9b1fca_add_error_reports_table.py` — the table this feature adds columns
  to.

### Likely changes

- A new Alembic migration adding `resolved_at TIMESTAMP(timezone=True) NULL` and `resolved_by TEXT NULL` to
  `error_reports` (mirroring `notifications.read_at`'s nullable-timestamp shape, plus who did it — the
  admin page needs to show/could show who addressed something, unlike the self-service notification-read
  case which doesn't need a "by" column since it's always "by the reading user" themselves).
  **Important: `backend/alembic/versions/` currently has two heads (`ddccca9b1fca` and `a7c9e2f4b1d3` — see
  `plan.md`'s "What we discussed"). This migration must either be preceded by an Alembic merge migration
  for those two heads, or itself be written as a merge (a migration with two `down_revision` entries) that
  also does the schema change — implementer's choice, but `alembic upgrade head` must work cleanly
  afterwards (single head).**
- `backend/src/error_reports/db_client.py` — add `mark_addressed(report_id, username) -> bool` (mirrors
  `NotificationsDB.mark_read`'s signature/return shape) and extend `list_all` with an
  `unaddressed_only: bool` (or equivalent) parameter.
- `backend/src/error_reports/endpoints.py` — add the resolve endpoint; extend the list endpoint's query
  params to filter by addressed state (matching how `notifications/endpoints.py`'s list already takes
  `unread_only`).
- `frontend_fastapi/backend_client.py` — add a `mark_error_report_addressed(report_id, username)` function
  and extend `list_error_reports` to accept the new filter, mirroring the existing
  `list_notifications`/`mark_notification_read` pair's shape exactly.
- `frontend_fastapi/routers/admin.py` — pass the toggle state through, add a POST route for the
  mark-addressed action (CSRF-protected, same as every other POST in this app via
  `deps.py`'s CSRF dependency).
- `frontend_fastapi/templates/admin/overview.html` — reorder, add indicator + mark-addressed button + pills.

## Acceptance criteria

### Scenario: Unaddressed reports show amber when none are urgent

**Given** two unaddressed error reports exist, neither marked `urgent`

**When** a staff user views the admin overview page

**Then** the error-reports section shows an amber indicator

### Scenario: A single urgent unaddressed report turns the indicator red

**Given** one unaddressed report is `urgent=True` (alongside any number of non-urgent unaddressed reports)

**When** a staff user views the admin overview page

**Then** the indicator is red, not amber

### Scenario: No unaddressed reports shows no indicator

**Given** every existing report has been marked addressed (or none exist)

**When** a staff user views the admin overview page

**Then** no amber/red indicator is shown, and the default (unaddressed-only) list view shows no rows

### Scenario: Marking a report addressed removes it from the default view

**Given** an unaddressed error report is visible on the admin page

**When** a staff user marks it addressed

**Then** it no longer appears in the default (unaddressed-only) list, the indicator recalculates (e.g. drops
from red to amber, or disappears entirely, depending on what else is still unaddressed), and it does appear
when the "show addressed" toggle is used

### Scenario: Addressed-by is recorded

**Given** staff user `alice` marks a report addressed

**When** the "show addressed" view is opened

**Then** the report shows it was addressed by `alice` (and when)

### Scenario: Section ordering on the admin page

**Given** any admin page load with data in every section

**When** the page renders

**Then** the sections appear in this order: Projects-by-status/Audit-chain, Expiring soon, Error reports &
feedback, Recent jobs

### Scenario: A non-staff user cannot mark a report addressed

**Given** a non-staff user (who cannot reach `/admin` at all today, per `require_data_custodian`)

**When** they attempt to POST directly to the new mark-addressed route

**Then** they are rejected the same way every other `require_data_custodian`-gated route already rejects
them (403/redirect, per existing `require_data_custodian` behaviour) — no new access-control code needed
beyond applying the existing dependency to the new route

## Edge cases

- Marking an already-addressed report addressed again: follow `NotificationsDB.mark_read`'s existing
  precedent (its `WHERE ... read_at IS NULL` guard makes a second call a no-op that returns `False`/404) —
  same idempotency shape here.
- A report with `job_id` pointing at a job that's since been deleted — already handled at the schema level
  (`ON DELETE SET NULL`, per the existing migration); unaffected by this feature.
- Sort order within the default (unaddressed) list: keep the existing `ORDER BY created_at DESC` — not
  something this feature needs to change, just confirm the new WHERE-clause filtering doesn't disturb it.

## Success

An admin can look at the error-reports section and immediately tell (via colour) whether anything needs
attention, act on individual reports one at a time, and have the list shrink to reflect what's actually
still outstanding.

## Failure behaviour

If `backend_client.list_error_reports()` fails, `admin.py`'s existing `backend_error` handling already
covers this (the whole page still renders with an error banner, per the current `admin_overview` function) —
no new failure path needed. If the mark-addressed POST fails (backend error), follow the existing
`frontend_fastapi` convention of a flash message + redirect back to the same page (see other POST handlers
in `research_projects.py`/`jobs.py` for the pattern), rather than a raw 500.

## Testing considerations

- `backend/tests/test_error_reports_db.py`/`test_error_reports_endpoints.py` already exist and cover
  `create`/`list_all`/the create endpoint — extend with cases for `mark_addressed` (marks correctly, is
  idempotent/no-op on a second call, `resolved_by` is recorded) and the filtered `list_all` (unaddressed vs
  addressed vs all), plus the new endpoint's happy path and 404-on-already-addressed case, following the
  same fixture/DB conventions those files already use (real Postgres, per `CLAUDE.md`'s Testing section).
- `frontend_fastapi/tests/test_admin.py` already exists — extend for: the reordered sections, the
  indicator's amber/red/none logic (mock `backend_client.list_error_reports` with the right shapes), the
  mark-addressed POST route (success + CSRF-protected + staff-gated, following existing POST-route test
  patterns elsewhere in this test file or `test_research_projects.py`).
- A new migration needs the same "does `alembic upgrade head` succeed from empty" check the existing test
  suite already implicitly relies on (migrations run automatically per `CLAUDE.md`'s Testing/Environment
  sections) — the two-heads issue noted above will surface here first if not resolved.

## Implementation notes

- Mirror `NotificationsDB`/`notifications/endpoints.py`'s shape as closely as sensible — this codebase has
  an established pattern for "persisted per-row acknowledgement," and reusing it keeps this feature
  boring and consistent rather than inventing a new one.
- The amber/red/none indicator logic is a small pure computation (count unaddressed, check if any are
  urgent) — no need for a dedicated backend aggregate endpoint; compute it in `admin.py` from the same
  `list_error_reports` response already fetched, the same way `admin.py`'s own docstring already justifies
  computing `project_status_counts` client-side rather than adding a backend aggregate for something this
  trivial.
- `urgent=True` reports still get no email/other dispatch, per `error_reports/endpoints.py`'s existing
  docstring (SMTP isn't wired up) — this feature doesn't change that; it only adds the addressed workflow.

## Out of scope

- Email or other real-time notification dispatch for urgent reports.
- Any change to how reports are created (`error_reports/report.html`, the submission form) — unaffected.
