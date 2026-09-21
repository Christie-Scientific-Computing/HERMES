# Feature F006: Approved export destinations & message ID

## Purpose

Export destination determines where a patient's data physically goes;
message ID determines which trial-anonymisation table gets used. Neither is
currently a governed, project-level concept — destination is chosen freely
per job at submission time, and message ID (pre-F002) was a client-editable
per-job field. This feature makes both project-level, reviewed at approval,
and changeable afterward only through an amendment that itself requires
approval — while the project keeps operating on its current, already-approved
values throughout that review.

## Behaviour

**At project creation**, the creator picks a destination type first — DICOM
SCP or ProKnow — then multi-selects from that type's own list (the existing
dynamic Orthanc modality list, or ProKnow collections). Selections of both
types may coexist on one project. The creator also sets a message ID
(optional, integer).

**At approval**, the reviewer sees the proposed destinations and message ID
alongside the rest of the project and can approve or reject the whole
submission as today (no separate sign-off step for these fields — they ride
the normal approve/reject decision).

**After approval**, destinations and message ID are locked — the project
detail page offers an "Amend" action instead of direct editing. Submitting
an amendment proposes new destinations/message ID without touching the
currently-active ones; the project keeps exporting to (and displaying) its
current values throughout. A reviewer sees old vs. proposed values and
approves or rejects the amendment. Approving swaps the active values to the
proposed ones; rejecting discards the proposal and changes nothing.

## Dependencies

None (this feature establishes the schema that F002 and F008 depend on).

## Relevant code

### Existing

- `frontend_fastapi/forms/research_projects.py` — `CreateProjectForm`/
  `ReviewProjectForm` (~lines 6-30) currently have only `title`,
  `description`, `ethics_reference`, and review `decision`/`comment`/
  `expiry_date` — no destination concept exists here today.
- `backend/src/export/logic.py` (`Exporter.__init__(self, destination:
  str)`, ~lines 69-70, comment "# DICOM SCP or collection") — confirms
  destination today is a bare string chosen per export, not project-scoped.
- `backend/src/export/endpoints.py` (~lines 59, 88-89, 307, 482) — passes
  `destination`/`destination_type` (`"dicom_modality"` or
  `"proknow_collection"`) per job; this vocabulary (`destination_type`
  values) should be reused for the new project-level rows, not reinvented.
- `frontend_fastapi/views.py` / `frontend_fastapi/backend_client.py`
  (`get_orthanc_modalities`, called from `frontend_fastapi/routers/jobs.py`
  ~lines 169-184 in the pre-rewrite codebase) — the existing dynamic DICOM
  modality list to reuse for the DICOM half of the picker.
- `ProKnowExportForm.collection` (`frontend_fastapi/forms/jobs.py`) — the
  existing source of ProKnow collection choices on the job form; reuse the
  same source for the ProKnow half of the picker rather than adding a second
  lookup.
- `backend/src/projects/db_client.py` (`ProjectsDB`) — `create`/`submit`/
  `review`/`revoke` methods to extend; existing lifecycle
  (`draft`/`submitted`/`approved`/`rejected`/`revoked`) that the amendment
  flow deliberately does **not** add a new status value to (see
  Implementation notes).
- `research_projects` schema (`CLAUDE.md`'s HermesDB Schema section):
  `project_id, title, description, ethics_reference, status, created_by,
  reviewed_by, review_comment, submitted_at, approved_at, expiry_date,
  created_at`.

### Likely changes

- New Alembic migration (`backend/alembic/versions/`):
  ```sql
  ALTER TABLE research_projects ADD COLUMN message_id INTEGER NULL;
  ALTER TABLE research_projects ADD COLUMN pending_message_id INTEGER NULL;

  CREATE TABLE project_destinations (
      id SERIAL PRIMARY KEY,
      project_id ... REFERENCES research_projects,
      destination_type TEXT NOT NULL,   -- 'dicom_modality' | 'proknow_collection'
      destination_value TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'proposed'
      added_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  ```
- `backend/src/projects/db_client.py` — extend `create()`/`submit()` to
  accept destinations + `message_id`, writing `project_destinations` rows
  with `status='active'` on initial approval. New methods:
  `propose_amendment(project_id, destinations, message_id)` (writes
  `status='proposed'` rows and sets `pending_message_id`, without touching
  `active` rows/`message_id`), `approve_amendment(project_id)` (deletes old
  `active` rows, promotes `proposed` → `active`, copies
  `pending_message_id` → `message_id`, clears `pending_message_id`),
  `reject_amendment(project_id)` (deletes `proposed` rows, clears
  `pending_message_id`).
- `backend/src/projects/endpoints.py` — extend create/approve request
  models; new endpoints for proposing/approving/rejecting an amendment.
- `frontend_fastapi/forms/research_projects.py` — destination-type toggle +
  two multi-selects, `message_id` field, on both the creation form and a new
  amendment form (can share a form class).
- `frontend_fastapi/templates/research_projects/` — creation form template
  gains the picker; review/approve template shows proposed values; project
  detail template gains an "Amend" action and a pending-amendment banner
  when `project_destinations` has any `status='proposed'` rows or
  `pending_message_id IS NOT NULL`.

## Acceptance criteria

### Scenario: Create a project with mixed destination types

**Given** a user creating a project

**When** they select two DICOM modalities and one ProKnow collection, and
set a message ID

**Then** on submission for review, all three destinations and the message
ID appear as the proposed configuration for the reviewer

### Scenario: Approval activates the configuration

**Given** a submitted project with destinations and a message ID pending
review

**When** a reviewer approves the project

**Then** those destinations and that message ID become the project's active
configuration, immediately visible on the project detail page and usable by
job submission (F002)

### Scenario: Amendment doesn't disrupt current operation

**Given** an approved project with active destinations `[A, B]`

**When** a member proposes an amendment changing the destinations to `[A,
C]`

**Then** the project detail page and job-submission form continue showing
`[A, B]` as active, and jobs submitted during this review continue targeting
`[A, B]`, until the amendment is decided

### Scenario: Approving an amendment swaps the active configuration

**Given** a pending amendment proposing `[A, C]` on a project currently
active on `[A, B]`

**When** a reviewer approves the amendment

**Then** the project's active destinations become `[A, C]`, and `B` is no
longer an active destination

### Scenario: Rejecting an amendment changes nothing

**Given** a pending amendment on a project currently active on `[A, B]`

**When** a reviewer rejects the amendment

**Then** the project's active destinations remain `[A, B]`, and the proposed
values are discarded

## Edge cases

- A project with zero destinations selected at submission — decide whether
  this is disallowed at the form level (likely yes, matching the intent
  that approval governs *something* real) or permitted as a valid "not
  exporting yet" state; recommend requiring at least one destination,
  consistent with the field's purpose.
- Two amendments proposed in quick succession before the first is reviewed —
  the second proposal should replace the first's `proposed` rows/value
  rather than stacking, since there is only one project detail page and one
  pending state to display.
- A message ID of `0` is a valid value distinct from "not set" (`NULL`) —
  ensure form/display logic distinguishes them rather than treating `0` as
  falsy/absent.

## Success

An end-to-end flow — create with destinations+message ID, submit, approve,
propose an amendment, verify the project stays on old values, approve the
amendment, verify it switches — passes both as a manual walkthrough and as
backend integration tests against `ProjectsDB`.

## Failure behaviour

Proposing an amendment on a project that isn't currently `approved` (e.g.
still `draft`/`submitted`, or already `rejected`/`revoked`) should be
rejected server-side with a clear error — amendments only make sense against
an active, approved configuration.

## Testing considerations

- `backend/tests/test_projects_db.py` — extend with cases for
  `propose_amendment`/`approve_amendment`/`reject_amendment`, mirroring the
  existing lifecycle-transition test style (`test_projects_db.py`'s
  approve/reject/revoke coverage).
- `backend/tests/test_projects_enforcement.py` — verify import/export
  enforcement (`require_project_member`) is unaffected by a pending
  amendment (i.e. the active configuration, not the proposed one, is what
  gates/targets real operations).
- `frontend_fastapi/tests/test_research_projects.py` — form rendering and
  submission tests for the two-step picker and the amendment flow.

## Implementation notes

**Why a `status` column instead of a new project lifecycle state**: adding
`amendment_pending` (or similar) to `research_projects.status` would ripple
through every place that already branches on the five existing statuses
(`is_project_active`, enforcement checks, the admin dashboard's status
counts) for a change that is really about the *destinations*, not the
project's own approval state — the project itself stays "approved" and
fully operational throughout. The `status` column on `project_destinations`
(`active`/`proposed`) plus `pending_message_id` keeps that distinction local
to this feature rather than touching the broader lifecycle machinery. See
D006 in `plan.md`.

Reuse `destination_type` values (`dicom_modality`/`proknow_collection`)
already established by `export/endpoints.py`, rather than inventing new
ones, so the vocabulary matches when F008 later reports stats grouped by
these values.

## Out of scope

- Enforcing that a job's chosen destination (at submission time) is one of
  the project's active destinations — the codebase's existing "Known Gaps"
  note (`CLAUDE.md`) already flags that any active project member can
  target any registered destination; tightening that is a separate,
  pre-existing gap (`docs/plans/safety-plan.md` §A), not part of this
  feature.
- A UI history of past amendments — only the current active/proposed state
  needs to be shown.
