# Feature F001: Set MU tolerance per job

## Purpose

Radiotherapy plans re-imported via Pinnacle need a monitor-unit (MU)
tolerance value for the export comparison PinnacleExport performs. Today
this value doesn't exist anywhere in HERMES's contract — it's presumably
hardcoded or absent inside the `PinnacleExport` submodule. This feature lets
a user set it per job from the HERMES job-submission form, with a sane
default, instead of it being invisible/unconfigurable.

## Behaviour

The job-submission form gains a plain numeric "MU tolerance" field —
top-level, not grouped into any "advanced" subsection, visible to any user
submitting a Pinnacle import. If left blank, the backend applies a
configured default. The value flows through to the payload HERMES hands to
PinnacleExport's `entry()` call.

## Dependencies

None.

## Relevant code

### Existing

- `backend/src/retrieve/logic.py` (`Importer.import_from_pinnacle`, ~lines
  299-313) — builds the `payload` dict (`remote_IP`, `remote_port`,
  `remote_AE_title`, `requests`) passed to `pinn_entry(payload)`. This is
  where `mu_tolerance` needs to join the payload.
- `backend/src/retrieve/endpoints.py` (`batch_import_file`, ~line 256) —
  already accepts `message_id: int | None = Form(None, ge=0, le=65535)` as a
  precedent for adding a new optional `Form(...)` parameter the same way.
- `frontend_fastapi/forms/jobs.py` (`JobSubmissionForm`, ~line 75) — existing
  field list (`scope`, `mrn`, `do_import`, `import_level`, `do_export`,
  `export_kind`, `destination`, `message_id`, `collection`) to add a sibling
  field to.
- `frontend_fastapi/templates/jobs/submit_job.html` (message_id rendered at
  ~lines 77-82, nested under `#field-dicom`) — template location for the new
  field; render at the same nesting level (visible whenever Pinnacle import
  is selected), not inside any collapsible/advanced wrapper.

### Likely changes

- `backend/src/retrieve/logic.py` — add `mu_tolerance` to the payload dict,
  reading a new env var (e.g. `MU_TOLERANCE_DEFAULT`) when the request
  didn't supply one.
- `backend/src/retrieve/endpoints.py` — `batch_import_file` (and any other
  entrypoint that calls `import_from_pinnacle`, e.g. the single-import path)
  gains `mu_tolerance: float | None = Form(None)`, passed through to the
  worker/logic call.
- `frontend_fastapi/forms/jobs.py` — new `mu_tolerance` `FloatField`,
  optional, no default value set client-side (backend fills the default when
  omitted, so the frontend needs no knowledge of its value).
- `frontend_fastapi/templates/jobs/submit_job.html` — add the input.
- `.env.example` / `CLAUDE.md`'s Environment Variables table — document
  `MU_TOLERANCE_DEFAULT`.

## Acceptance criteria

### Scenario: Default applies when left blank

**Given** a user submitting a Pinnacle import job leaves the MU tolerance
field blank

**When** the job is submitted

**Then** the payload sent toward PinnacleExport carries the configured
`MU_TOLERANCE_DEFAULT` value as `mu_tolerance`

### Scenario: User-supplied value overrides the default

**Given** a user enters a specific MU tolerance value on the job form

**When** the job is submitted

**Then** the payload carries that user-supplied value, not the default

### Scenario: Field is visible without opening anything

**Given** a user viewing the job-submission form with Pinnacle import
selected

**When** the page renders

**Then** the MU tolerance field is visible directly on the form, not hidden
behind a collapsed/advanced section

## Edge cases

- No `MU_TOLERANCE_DEFAULT` env var configured — decide whether the backend
  should refuse the job (fail closed) or proceed without the field at all;
  since PinnacleExport doesn't consume this value yet either way (see
  Implementation notes), proceeding without it is acceptable for now.
- Negative or zero MU tolerance — no validated "sane range" exists in this
  domain per the grilling session; leave unvalidated beyond basic
  float-parsing for this round rather than inventing a clinical bound.

## Success

A Pinnacle-import job submission includes an `mu_tolerance` value (explicit
or defaulted) in the payload logged/passed by `import_from_pinnacle`,
verifiable via a backend unit test on `Importer.import_from_pinnacle`'s
payload construction.

## Failure behaviour

An invalid (non-numeric) value in the form field fails standard WTForms
validation with a field-level error, consistent with how other numeric
fields on this form behave today (e.g. `message_id`'s prior `NumberRange`
validator).

## Testing considerations

- Backend: extend or add a test alongside `backend/tests/test_cleanup_orthanc.py`'s
  neighbours for `retrieve/logic.py`, asserting the payload dict contains
  `mu_tolerance` with the expected value (explicit and default-applied
  cases).
- Frontend: `frontend_fastapi/tests/test_jobs.py` — assert the new field
  renders and that a submitted value reaches the backend call
  (`backend_client` call assertions, following existing patterns in that
  test file for `message_id` before its removal in F002).

## Implementation notes

**PinnacleExport's own `entry()` call does not yet accept `mu_tolerance`** —
this is an external git submodule (`backend/src/retrieve/PinnacleExport/`)
not modified by this feature. Threading the value through HERMES's contract
is still worthwhile (it's the visible, testable part of this feature), but
the value has no real clinical effect until PinnacleExport's own source gets
a matching parameter — track that as a separate, external follow-up.

This feature was originally requested bundled with message ID as one
"advanced options" UI section; that framing was explicitly dropped during
design — see D001 in `plan.md`. Do not reintroduce a collapsible/advanced
grouping for this field.

## Out of scope

- Any change to the `PinnacleExport` submodule itself.
- Message ID — see F002, a separate, unrelated feature after the D001 split.
