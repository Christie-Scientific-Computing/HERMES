# Feature F007: Optional patient-ID-list upload at project creation

## Purpose

Reviewers currently have no record of which patients a project actually
intends to work with — a project is approved for unrestricted access to any
patient a member later chooses to import. An optional upload of the
requested patient list at creation time gives reviewers (and, later, F008's
"Requested" stat) a concrete number to work from, without making it
mandatory.

## Behaviour

Project creation gains an optional file upload: a plain text or
single-column CSV list of MRNs, one per line. If a creator doesn't upload a
list, they see a non-blocking warning suggesting they do, but can still
submit. Uploaded MRNs are stored against the project for later reporting
(F008).

## Dependencies

None.

## Relevant code

### Existing

- `frontend_fastapi/models.py` (`ProjectDocument`, ~lines 82-96) and its
  upload flow in `frontend_fastapi/routers/research_projects.py` (~lines
  266-325) — the closest existing upload precedent: streaming chunked write
  with mid-copy rejection (`_save_document_sync`, ~lines 266-300), a
  `_MAX_DOCUMENT_SIZE_BYTES` cap (~line 60, 50MB). Follow this pattern for
  the upload mechanics, but note it has **no file-type/content validation**
  today — this feature needs its own basic check (plain text/CSV, parseable
  MRN lines), since `ProjectDocument`'s upload is otherwise unvalidated.
- `research_projects` schema — no existing table for requested patients;
  this is new.

### Likely changes

- New Alembic migration (`backend/alembic/versions/`):
  ```sql
  CREATE TABLE project_requested_patients (
      project_id ... REFERENCES research_projects,
      mrn TEXT NOT NULL,
      added_at TIMESTAMPTZ NOT NULL DEFAULT now()
  );
  ```
  This is backend/HermesDB-owned, **not** local to `frontend_fastapi` like
  `ProjectDocument` — it needs to be queryable by F008's stats query, which
  lives backend-side.
- `backend/src/projects/db_client.py` — new
  `add_requested_patients(project_id, mrns: list[str])` and
  `count_requested_patients(project_id)`.
- `backend/src/projects/endpoints.py` — new endpoint accepting the parsed
  MRN list (or the raw file) at project-creation time.
- `frontend_fastapi/forms/research_projects.py` — optional `FileField` on
  the creation form; basic content validation (line-based text/CSV, each
  line plausibly an MRN — reject binary/unparseable uploads) before posting
  to the new backend endpoint.
- `frontend_fastapi/templates/research_projects/` — creation form template
  gains the upload field and, when it's left empty at submission, a
  non-blocking warning message.

## Acceptance criteria

### Scenario: Upload succeeds

**Given** a user creating a project attaches a text file with one MRN per
line

**When** they submit the form

**Then** the project is created and every MRN from the file is recorded
against it, retrievable via `count_requested_patients`

### Scenario: No upload still allows submission, with a warning

**Given** a user creating a project leaves the patient-list field empty

**When** they reach the submission step

**Then** they see a warning suggesting they upload a list, but submission
still succeeds if they proceed anyway

### Scenario: Malformed upload is rejected

**Given** a user attaches a file that isn't parseable as a plain-text/CSV
MRN list (e.g. a binary file)

**When** they submit

**Then** the form shows a validation error and the project is not created
with a partial/garbage patient list

## Edge cases

- Duplicate MRNs within one uploaded file — store as-is or de-duplicate;
  recommend de-duplicating at insert time so F008's "Requested" count
  reflects distinct patients, not upload artefacts.
- A very large file — reuse `ProjectDocument`'s existing size-cap pattern
  rather than inventing a new limit.
- Empty file (zero valid MRN lines) — treat the same as "no upload":
  proceed, but perhaps still show the warning rather than silently
  succeeding with a "0 requested" project.

## Success

A project created with an uploaded patient list has that list's row count
retrievable via `count_requested_patients`, matching the number of distinct
valid MRN lines in the uploaded file; a project created without one shows
the warning and still creates successfully.

## Failure behaviour

A malformed/unparseable upload fails validation before any database write —
no partially-recorded patient list.

## Testing considerations

- `frontend_fastapi/tests/test_research_projects.py` — form validation
  tests for valid/malformed/empty uploads, and the no-upload warning path.
- `backend/tests/test_projects_db.py` — `add_requested_patients`/
  `count_requested_patients` round-trip test, including the duplicate-MRN
  case.

## Implementation notes

Storing this backend-side (unlike `ProjectDocument`, which is
`frontend_fastapi`-local) is deliberate — F008's "Requested" stat is
computed backend-side alongside the other two stats, and having the source
data live anywhere else would mean the backend querying across two separate
databases for one stats endpoint.

## Out of scope

- Editing or removing individual MRNs from an already-uploaded list after
  creation — not requested; treat the list as fixed at creation for this
  round.
- Validating that uploaded MRNs actually exist in Mosaiq/Pinnacle/ProKnow —
  this is a request record, not a pre-flight check.
