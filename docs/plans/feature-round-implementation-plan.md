# Feature round — implementation plan

**Status:** design confirmed, not yet built. Source: `FEATURES.md` (original
requests) + a grilling session covering all open decisions (transcript in
`GRILLING.md`). Seven independent-ish work items; sequencing and per-item
detail below.

**Build order:** 02 → 03 → 05 → 04 → 06 → 01. Item 04 hard-depends on 05
(needs its schema). Item 07 was raised after ordering — fully independent,
safe to build any time, including first if it's the smallest PR wanted.

Items 01 and 05 both touch `research_projects`; see "Cross-cutting: schema"
at the end before starting either.

## 01 — Job settings: MU tolerance & message ID

**Schema:** none beyond 05's `research_projects.message_id` column (below) —
`message_id` lives there now, not on a job.

**Backend:**
- New env var `MU_TOLERANCE_DEFAULT` (float), read where `backend/src/retrieve/logic.py`
  builds the `import_from_pinnacle` payload (currently lines ~303-309:
  `remote_IP`/`remote_port`/`remote_AE_title`/`requests`). Add `mu_tolerance`
  to that payload, sourced from the request if given, else the env default.
- `batch_import_file` (`backend/src/retrieve/endpoints.py`) gains
  `mu_tolerance: float | None = Form(None)`.
- **Remove** `message_id` as a client-supplied `Form(...)` param on
  `batch_import_file`. Instead, when building the `chain_export` block, look
  up `message_id` server-side from `ProjectsDB.get(project_id)` — never trust
  a browser-supplied value for it, since it drives anonymisation-table
  selection.
- **Out of scope here:** PinnacleExport's own `entry()` call needs a matching
  parameter added to actually consume `mu_tolerance` — separate repo, tracked
  as a follow-up, not blocking this PR (the field can be threaded through and
  ignored downstream until that lands).

**Frontend:**
- `JobSubmissionForm` (`frontend_fastapi/forms/jobs.py`): remove the
  `message_id` `IntegerField` (currently ~line 110); add a plain
  `mu_tolerance` `FloatField`, optional, no grouping/collapsible section —
  a normal field alongside the others. Leave blank by default; the backend
  fills in `MU_TOLERANCE_DEFAULT` when omitted, so the frontend needs no
  knowledge of the default's value.
- `submit_job.html`: remove the message-ID input; add a read-only line next
  to the destination selector showing the project's `message_id` (template
  already has `project` in context).

## 02 — Change your own password

**Schema:** none (local to `frontend_fastapi`'s own DB).

**Backend/frontend** (all in `frontend_fastapi/`):
- New `ChangePasswordForm` (`forms/accounts.py`): `old_password`,
  `password1`, `password2` (`EqualTo`), reusing `security.password_strength_errors`
  for the new password (same validation `ActivateForm`/`CreateUserForm` use).
- New routes in `routers/accounts.py`: `GET/POST /accounts/me`, gated by
  `require_login`. On POST: verify `old_password` against `user.password_hash`
  via the existing argon2 hasher, validate the new password, `security.hash_password`
  + save, then invalidate every other session row for that user (new
  `SessionsDB`-style method — sessions are hand-rolled/DB-backed here with no
  automatic password-tied invalidation, so this has to be an explicit delete).
- `templates/accounts/change_password.html` — copy `activate.html`'s form
  markup.
- `templates/base.html:132` — turn the plain username `<span>` into
  `<a href="{{ url_for('accounts.me') }}">`.

Scope is a bare change-password form only — no broader account page yet.

## 03 — Patient event/task timeline

**Schema:** none.

**Backend:**
- `results/endpoints.py`'s `patient_timeline` (or `StatusDB.get_patient_history`'s
  caller) changes shape: instead of returning raw `events` rows, pair them
  into one record per attempt: `{stage, attempt, start_ts, end_ts, outcome,
  error_message}`.
  - Where `events.task_id` is set (queue-driven jobs), join to
    `tasks.started_at`/`tasks.finished_at` directly — no pairing logic
    needed, `finished_at IS NULL` means still running.
  - Where `task_id IS NULL` (the still-synchronous single-item path), pair
    the `start` row with its `success`/`failure` row by
    `(job_id, mrn, stage, attempt)`, same key `add_event` already writes.
  - `outcome` is `success` / `failure` / `cancelled` / `in_progress`.

**Frontend:**
- `patient_detail.html` timeline table: columns become **Start Time** /
  **End Time** / Stage / Event / Attempt / Error — replacing When / Stage /
  Event / Attempt / Error. `End Time` renders "In progress" when
  `end_ts is None` and not cancelled, "—" when cancelled with no natural end.
  Needs a Jinja timestamp filter (e.g. `{{ start_ts.strftime('%Y-%m-%d at %H:%M:%S') }}`)
  since there's no Django `date` filter here.

## 04 — Project homepage stats

**Depends on 05** (needs `project_destinations` and `project_requested_patients`).
No placeholder version ships before then.

**Backend:**
- New `ProjectsDB` (or `StatusDB`) query, one new endpoint
  `GET /projects/{project_id}/stats`:
  - **Requested** — `COUNT(*) FROM project_requested_patients WHERE project_id = %s`.
  - **Restored** (imported) — distinct-MRN count of successful `retrieve`
    events for the project's jobs, same predicate `list_recent_jobs_with_counts`
    already uses, scoped by `jobs.project_id`.
  - **Sent to <destination>** — one row per destination: distinct-MRN count
    of successful `export` events, grouped by destination. Confirm where
    destination lives on an export event today (`events.details->>'destination'`)
    before writing the `GROUP BY`.

**Frontend:**
- `research_projects.py`'s `project_detail` calls the new endpoint.
- `detail.html`: new "Project overview" card beside "Approval status" with
  the three stats (destination stat broken out per destination, not summed).
  Expiry date colour-coded reusing `deps.expiring_soon`'s existing 30-day
  threshold for amber; add a tighter red threshold (e.g. <7 days) — pick the
  exact cutover at implementation time, no need to re-confirm with the user.

## 05 — Project creation & approval fields

**Schema** (one Alembic migration, HermesDB):
```
ALTER TABLE research_projects ADD COLUMN message_id INTEGER NULL;

CREATE TABLE project_destinations (
    id SERIAL PRIMARY KEY,
    project_id ... REFERENCES research_projects,
    destination_type TEXT NOT NULL,   -- 'dicom' | 'proknow'
    destination_value TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'proposed'
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE project_requested_patients (
    project_id ... REFERENCES research_projects,
    mrn TEXT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The `status` column on `project_destinations` is how "stays live on current
destinations until an amendment is approved" is implemented: an amendment
inserts new rows with `status='proposed'` alongside the existing
`status='active'` ones; approving the amendment flips old `active` rows to
inactive/deleted and promotes the `proposed` ones; rejecting just deletes the
`proposed` rows. Same applies to `message_id` — track a proposed value
(e.g. `research_projects.pending_message_id`) until approved.

**Backend:**
- `ProjectsDB`: `create()`/`submit()` extended to accept destinations +
  `message_id`; new `propose_amendment()`, `approve_amendment()`,
  `reject_amendment()`; `add_requested_patients()` (parses an uploaded
  CSV/text list, one MRN per line).
- `research_projects/endpoints.py`: extend create/approve request models;
  new amendment endpoints; new upload endpoint.

**Frontend:**
- Creation form: `description` → `DataRequired()`. Destination picker is
  two-step — a type toggle (DICOM SCP / ProKnow) revealing the matching
  multi-select (Orthanc modalities via the existing `get_orthanc_modalities`
  call, or ProKnow collections); selections from both types can coexist.
  Optional file field for the patient-ID list, following `ProjectDocument`'s
  streaming/size-cap upload pattern (new subdir, add a basic CSV/text
  content check — `ProjectDocument` itself has none today).
- Review/approve page: show proposed destinations/`message_id` alongside
  current ones when reviewing an amendment.
- Project detail page: an "Amend" action opening the same destination
  picker, submitting a proposal; a banner when an amendment is pending.

## 06 — Error reporting & suggestions

**Schema** (one Alembic migration, HermesDB):
```
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
Mirrors the existing `notifications` table shape.

**Backend:**
- New `backend/src/error_reports/db_client.py` (`ErrorReportsDB`) +
  `endpoints.py` (`POST /error_reports`, `GET /error_reports` — internal-key
  gated, same pattern as `notifications/endpoints.py`), registered in
  `backend/main.py`.

**Frontend:**
- New `frontend_fastapi/routers/error_reports.py`: `GET/POST /report`
  (category radio, urgent checkbox, optional `job_id`, free-text message),
  `require_login`, `username` auto-attached.
- Admin log: new table section (reuses the admin dashboard's existing
  table-card template idiom), gated by `require_data_custodian`.
- **No email dispatch.** `frontend_fastapi`'s SMTP isn't functional today
  despite the settings existing — `urgent` is recorded but nothing sends.
  Revisit once SMTP is actually fixed; the report form/table need no changes
  when it is, just a call to `email_backend.send_mail(...)` on create.

## 07 — Frontpage jobs table

Independent of everything else above.

**Backend:**
- Fix the root cause of the clutter: `retrieve/endpoints.py`'s
  `description=f"Batch import from {tmp_path}"` (~line 299) leaks a server
  temp filepath. Change to use `file.filename` only, e.g.
  `f"Batch import ({file.filename})"`.
- Add a `project_id`-scoped variant of `StatusDB.list_recent_jobs_with_counts`
  (or an optional `project_id` filter param on the existing one) — the admin
  dashboard already has the richer query; the frontpage just needs it scoped
  per-project instead of global.

**Frontend:**
- `jobs.py`'s `dashboard` view calls the counts-aware query instead of
  `list_project_jobs`.
- `dashboard.html` columns: Description | Created | Imported | Exported.

## Cross-cutting: schema

Items 01 and 05 both add columns to `research_projects` (`message_id` in 05's
migration; 01 only reads it). Write and ship 05's migration first — 01's
backend change (look up `message_id` server-side) has nothing to read until
then. If built in the 02→03→05→04→06→01 order above this is automatic.

## Out of scope / follow-ups

- PinnacleExport submodule needs its own change to accept `mu_tolerance` in
  `entry()`'s payload — separate repo/PR.
- Error-report email-on-urgent, once `frontend_fastapi`'s SMTP is actually
  working.
- Exact red/amber day thresholds for expiry colour-coding (04) — pick a
  sensible value at implementation time, not a re-confirmation point.
