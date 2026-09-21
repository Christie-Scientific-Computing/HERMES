# Feature F002: Display project message ID on job form

## Purpose

Message ID is a live trigger for which trial-anonymisation table gets used
downstream — it must not be something a user can silently change per job.
Today it's a client-editable `IntegerField` on the job form. This feature
makes it a read-only display of the value governed at the project level
(F006), so users can see which message ID a job will use without being able
to override it.

## Dependencies

- **F006 — Approved export destinations & message ID**: this feature has
  nothing to read or display until `research_projects` has a governed
  `message_id` value. Build F006 first.

## Behaviour

The job-submission form no longer accepts a message ID as user input.
Instead, next to the destination selection, the form shows the current
project's message ID as plain read-only text (or "not set" if the project
has none configured). The value submitted to the backend is never
client-supplied — the backend looks it up server-side from the project
record.

## Relevant code

### Existing

- `frontend_fastapi/forms/jobs.py` (`JobSubmissionForm`, `message_id`
  `IntegerField`, ~lines 110-115, `NumberRange(0, 65535)`) — field to remove.
- `frontend_fastapi/templates/jobs/submit_job.html` (~lines 77-82) — where
  the input currently renders; replace with a read-only display.
- `frontend_fastapi/routers/jobs.py` (~lines 171, 182) — passes
  `message_id=form.message_id.data` into `_enqueue_batch_job`; remove.
- `backend/src/retrieve/endpoints.py`'s `batch_import_file` (~line 256,
  `message_id: int | None = Form(None, ge=0, le=65535)`) — remove as a
  client-supplied parameter.
- `backend/src/projects/db_client.py`'s `ProjectsDB` — where the governed
  `message_id` value (added by F006) is read from.

### Likely changes

- `frontend_fastapi/forms/jobs.py` — remove the `message_id` field entirely.
- `frontend_fastapi/templates/jobs/submit_job.html` — add a read-only line
  near the destination selector: `Message ID: {{ project.message_id or
  "not set" }}`.
- `frontend_fastapi/routers/jobs.py` — stop reading/forwarding
  `form.message_id`.
- `backend/src/retrieve/endpoints.py` — `batch_import_file` stops accepting
  `message_id` as a `Form(...)` parameter; instead looks up
  `ProjectsDB.get(project_id).message_id` when building the `chain_export`
  block.

## Acceptance criteria

### Scenario: Message ID is shown, not editable

**Given** a project with `message_id` set to `1234`

**When** a member of that project opens the job-submission form

**Then** "1234" is displayed next to the destination selector as read-only
text, with no input field to change it

### Scenario: Submitted job uses the project's message ID, not a client value

**Given** a project with `message_id` set to `1234`

**When** a job is submitted for that project (regardless of any value a
malicious or stale client might attempt to send)

**Then** the backend's `chain_export` block for that job carries `1234`,
sourced from the project record, never from request input

### Scenario: No message ID configured

**Given** a project with no `message_id` set

**When** a member opens the job-submission form

**Then** the display shows "not set" (or equivalent), and no `message_id`
key is added to the job's `chain_export` block

## Edge cases

- A project's `message_id` changes between form-render and submission (e.g.
  an amendment is approved mid-session) — since the value is looked up
  server-side at submission time, the job always uses the value current at
  submission, not whatever was displayed when the page loaded. Worth a code
  comment noting this is deliberate, not a race bug.

## Success

Submitting a job with `do_export`/chained export enabled produces a
`chain_export` payload whose `message_id` matches the project's current
value, with no code path accepting a client-supplied override.

## Failure behaviour

N/A — this is a display + server-side-lookup change with no new failure
mode; removing a client input field cannot itself fail.

## Testing considerations

- `frontend_fastapi/tests/test_jobs.py` — update/remove any existing
  assertions about a submittable `message_id` field; add an assertion that
  the rendered page shows the project's value as text.
- `backend/tests/` — extend `test_projects_endpoints.py` or the retrieve
  endpoint tests to assert `message_id` is sourced from `ProjectsDB`, and
  that a `message_id` value in the raw request body (if sent) is ignored.

## Implementation notes

This is a security-relevant removal, not just a UI simplification: the point
is that message ID cannot be influenced by request data at all once this
ships. Confirm the backend endpoint change actually drops the `Form(...)`
parameter rather than merely ignoring it while still declaring it (a
declared-but-ignored parameter is confusing and invites a future regression
where someone starts using it again).

## Out of scope

- MU tolerance — see F001, a separate, unrelated feature after the D001
  split.
- The amendment workflow governing how `message_id` changes — see F006.
