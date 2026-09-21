# Feature F009: Submit an error report

## Purpose

There's currently no channel for users to report bugs, suggestions, or data
leaks (e.g. a patient ID visibly shown in an error message) from inside the
app. This feature adds a simple submission form, backend-recorded, so
reports don't depend on an out-of-band channel like email or Slack.

## Behaviour

A logged-in user can open a report form with: a category (feedback or
error), an "urgent" flag, an optional related job ID, and a free-text
message. Submitting records the report against their username. No
notification is sent on submission this round (see Deferred decisions in
`plan.md`) — the `urgent` flag is recorded for F010's admin view to
surface, not acted on immediately.

## Dependencies

None.

## Relevant code

### Existing

- `backend/src/notifications/db_client.py` (`NotificationsDB`) and its
  table (`notifications(id, username, kind, message, job_id, project_id,
  created_at, read_at)`, created in
  `backend/alembic/versions/3cea980979d2_add_admin_dashboard_and_notifications.py:45-56`)
  — the direct shape precedent for `error_reports`: nullable `job_id`
  foreign key, `username`/`message`/`created_at`, `create()`/`list_for_user()`-style
  methods.
- `backend/src/notifications/endpoints.py` — internal-key-gated router
  pattern to follow for the new `error_reports` endpoints.
- `frontend_fastapi/deps.py` (`require_login`) — gates the new route; every
  other route in this app already requires login, so an anonymous
  submission path would be a new, unrequested access pattern.
- `frontend_fastapi/backend_client.py` (~lines 358-371, the
  `list_notifications`/`mark_notification_read` block) — the `_get`/`_post`
  helper pattern to extend for the new endpoint calls.

### Likely changes

- New Alembic migration (`backend/alembic/versions/`):
  ```sql
  CREATE TABLE error_reports (
      id SERIAL PRIMARY KEY,
      username TEXT NOT NULL,
      category TEXT NOT NULL,       -- 'feedback' | 'error'
      urgent BOOLEAN NOT NULL DEFAULT false,
      message TEXT NOT NULL,
      job_id ... NULL REFERENCES jobs,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  ```
- New `backend/src/error_reports/db_client.py` (`ErrorReportsDB`,
  `create()`/`list()`) and `backend/src/error_reports/endpoints.py` (`POST
  /error_reports`), registered in `backend/main.py` alongside the other
  routers.
- New `frontend_fastapi/routers/error_reports.py`: `GET/POST /report`,
  `require_login`, auto-attaches `username` from the session (never
  client-supplied).
- New form in `frontend_fastapi/forms/` (or inline in the router): category
  radio, urgent checkbox, optional job-ID field, free-text message.
- New template `frontend_fastapi/templates/error_reports/submit.html`.

## Acceptance criteria

### Scenario: Submit a basic report

**Given** a logged-in user opens the report form

**When** they select "feedback", leave urgent unchecked, leave job ID blank,
and enter a message

**Then** a new `error_reports` row is created with their username, category
`feedback`, `urgent=false`, `job_id=NULL`, and their message

### Scenario: Submit an urgent report tied to a job

**Given** a logged-in user opens the report form while viewing a specific
job

**When** they select "error", check "urgent", enter that job's ID, and a
message describing a data leak

**Then** a new `error_reports` row is created with `category='error'`,
`urgent=true`, and the given `job_id`

### Scenario: Message is required

**Given** a logged-in user opens the report form

**When** they submit with an empty message

**Then** the form re-renders with a validation error and no report is
created

### Scenario: Reporter identity is never client-supplied

**Given** any submitted report

**When** it is recorded

**Then** its `username` is always the submitting session's own username,
never a value read from request body/form data

## Edge cases

- An invalid/nonexistent `job_id` entered by the user — decide whether to
  validate it exists (extra round-trip) or accept it as free-form text
  matched loosely; recommend accepting it without strict FK validation at
  the form level, since misreporting a job ID is a minor inconvenience, not
  a correctness/safety issue, and a hard FK constraint at the database level
  (if used) already prevents outright garbage.
- Very long free-text messages — apply a reasonable column-level length
  cap; no specific requirement was given, so pick a generous bound (e.g.
  10,000 characters) rather than leaving it unbounded.

## Success

A logged-in user can submit a report through the UI and see it recorded,
verifiable via `ErrorReportsDB.list()` or directly in the `error_reports`
table, with the exact category/urgent/message/job_id values they entered
and their own username attached.

## Failure behaviour

A missing required field (message) is an ordinary form validation error,
consistent with every other form in this app.

## Testing considerations

- Backend: new `backend/tests/test_error_reports_db.py` (mirroring
  `test_notifications_db.py`'s structure) and
  `test_error_reports_endpoints.py` (mirroring
  `test_notifications_endpoints.py`).
- Frontend: new `frontend_fastapi/tests/test_error_reports.py` (mirroring
  `test_notifications.py`'s structure) covering form rendering, successful
  submission, and validation failure.

## Implementation notes

No email is sent on submission this round regardless of the `urgent` flag —
`frontend_fastapi`'s SMTP isn't functional today despite the settings
existing (confirmed during grilling). The `urgent` flag is still recorded
so F010's admin view can surface it, and so no schema change is needed
later when email is actually wired up.

## Out of scope

- Sending any notification/email on submission (deferred — see `plan.md`).
- Editing or withdrawing a submitted report.
